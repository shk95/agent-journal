import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import time
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import OBSERVATION_LEASE, PROJECT, SERVER_VERSION_HEADER, turn_start
from .graph import graph, layout
from .text_layout import markdown_with_title, note_title

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects(
    project TEXT PRIMARY KEY, since INTEGER NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns(
    project TEXT NOT NULL REFERENCES projects(project), thread TEXT NOT NULL, turn TEXT NOT NULL,
    started INTEGER NOT NULL, status TEXT NOT NULL, prompt TEXT NOT NULL, summary TEXT NOT NULL,
    parent TEXT, items TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(project, thread, turn)
);
CREATE INDEX IF NOT EXISTS journey_order ON turns(project, started, thread, turn);
CREATE INDEX IF NOT EXISTS session_journey_order ON turns(project, thread, started, turn);
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(project),
    thread TEXT, turn TEXT, body TEXT NOT NULL, created REAL NOT NULL,
    FOREIGN KEY(project,thread,turn) REFERENCES turns(project,thread,turn)
);
"""
STATIC = Path(__file__).parent / "static"
SERVER_VERSION = version("agent-journal")
STORAGE_WINDOW_DAYS = 30


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def listing(db, sql, values=()):
    return [dict(row) for row in db.execute(sql, values)]


TURN_COLUMNS = (
    "thread,turn,parent,parent_turn,started,status,prompt,summary,updated,since,active"
)
NOTE_COLUMNS = "id,project,thread,turn,title,body,created,version,priority"
NOTE_LIST_COLUMNS = "id,project,thread,turn,title,substr(body,1,200) AS preview,created,version,priority"


def note_priority(value):
    if value not in ("highlighted", "normal", "low"):
        raise ValueError("Invalid note priority")
    return value
BOARD_PAGE_SIZE = 60


def with_observation(rows):
    now = time.time()
    for row in rows:
        updated, since, active = (
            row.pop(key) for key in ("updated", "since", "active")
        )
        if row["status"] == "inProgress":
            if not active or turn_start(row["turn"], row["started"]) < since:
                row["observation"] = "stopped"
            elif updated is None or updated < now - OBSERVATION_LEASE:
                row["observation"] = "unavailable"
            else:
                row["observation"] = "observed"
    return rows


def cursor_arg(value, length):
    cursor = json.loads(value)
    if not isinstance(cursor, list) or len(cursor) != length:
        raise ValueError("Invalid page cursor")
    return cursor


def journey(db, project, args, *, board=False, inclusive=False):
    columns = (
        "thread,turn,parent,parent_turn,started,status,substr(prompt,1,2000) AS prompt,"
        "summary,updated,since,active,revision"
        if board else TURN_COLUMNS
    )
    sql = (
        f"SELECT {columns} FROM turns JOIN projects USING(project) WHERE project=?"
    )
    values = [project]
    if args.get("thread"):
        sql += " AND thread=?"
        values.append(args["thread"])
    if args.get("exclude_turn"):
        sql += " AND turn<>?"
        values.append(args["exclude_turn"])
    forward = board and (args.get("after") or args.get("edge") == "first")
    page_key = "after" if forward else "before"
    if value := args.get(page_key):
        operator = ">" if forward else "<"
        if inclusive:
            operator += "="
        sql += f" AND (started,turn,thread){operator}(?,?,?)"
        values.extend(cursor_arg(value, 3))
    order = "ASC" if forward else "DESC"
    sql += f" ORDER BY started {order},turn {order},thread {order}"
    if args.get("limit"):
        sql += " LIMIT ?"
        values.append(max(1, min(200, int(args["limit"]))))
    rows = listing(db, sql, values)
    if not forward:
        rows.reverse()
    rows = with_observation(rows)
    if args.get("lanes") == "true":
        for row, lanes in zip(rows, layout(rows)):
            row["graph_lanes"] = lanes
    else:
        for row, line in zip(rows, graph(rows)):
            row["graph"] = line
    return rows


def board_page(db, project, args):
    edge = args.get("edge")
    thread = args.get("thread")
    thread_params = (thread,) if thread else ()
    inclusive = False
    turn_args = dict(args, lanes="true", limit=BOARD_PAGE_SIZE + 1)
    if edge in ("first-note", "last-note"):
        order = "ASC" if edge == "first-note" else "DESC"
        target = db.execute(
            f"""SELECT t.started, t.turn, t.thread FROM notes AS n
                JOIN turns AS t USING(project,thread,turn)
                WHERE n.project=? AND n.turn IS NOT NULL
                {"AND n.thread=?" if thread else ""}
                ORDER BY t.started {order}, t.turn {order}, t.thread {order} LIMIT 1""",
            (project, *thread_params),
        ).fetchone()
        turn_args.pop("before", None)
        turn_args.pop("after", None)
        if target:
            turn_args["after" if edge == "first-note" else "before"] = json.dumps(list(target))
            inclusive = True
    turns = journey(db, project, turn_args, board=True, inclusive=inclusive)
    forward = bool(turn_args.get("after") or edge == "first")
    turns = turns[:BOARD_PAGE_SIZE] if forward else turns[-BOARD_PAGE_SIZE:]

    def has_turn(operator, row):
        return db.execute(
            f"""SELECT 1 FROM turns WHERE project=? {"AND thread=?" if thread else ""}
                AND (started,turn,thread){operator}(?,?,?) LIMIT 1""",
            (project, *thread_params, row["started"], row["turn"], row["thread"]),
        ).fetchone() is not None

    turn_notes = []
    if turns:
        slots = ",".join("(?,?)" for _ in turns)
        keys = [value for row in turns for value in (row["thread"], row["turn"])]
        turn_notes = listing(
            db,
            f"""WITH visible(t_thread, t_turn) AS (VALUES {slots})
                SELECT {NOTE_LIST_COLUMNS} FROM visible
                CROSS JOIN notes INDEXED BY notes_by_target
                WHERE project=? AND thread=t_thread AND turn=t_turn
                ORDER BY created,id""",
            (*keys, project),
        )

    note_forward = bool(args.get("project_after") or args.get("project_edge") == "first")
    note_order = "ASC" if note_forward else "DESC"
    note_cursor = args.get("project_after" if note_forward else "project_before")
    note_where = "project=? AND turn IS NULL"
    note_values = [project]
    if note_cursor:
        note_where += " AND (created,id)>(?,?)" if note_forward else " AND (created,id)<(?,?)"
        note_values.extend(cursor_arg(note_cursor, 2))
    project_notes = listing(
        db,
        f"""SELECT {NOTE_LIST_COLUMNS} FROM notes WHERE {note_where}
            ORDER BY created {note_order}, id {note_order} LIMIT ?""",
        (*note_values, BOARD_PAGE_SIZE),
    )
    if not note_forward:
        project_notes.reverse()

    def has_project_note(operator, row):
        return db.execute(
            f"""SELECT 1 FROM notes WHERE project=? AND turn IS NULL
                AND (created,id){operator}(?,?) LIMIT 1""",
            (project, row["created"], row["id"]),
        ).fetchone() is not None

    data = {
        "turns": turns,
        "turn_notes": turn_notes,
        "project_notes": project_notes,
        "earlier": bool(turns) and has_turn("<", turns[0]),
        "later": bool(turns) and has_turn(">", turns[-1]),
        "project_earlier": bool(project_notes) and has_project_note("<", project_notes[0]),
        "project_later": bool(project_notes) and has_project_note(">", project_notes[-1]),
    }
    revision = hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()
    if args.get("revision") == revision:
        return {"revision": revision, "unchanged": True}
    return data | {"revision": revision}


def read_note(db, project, identifier):
    row = db.execute(
        f"SELECT {NOTE_COLUMNS} FROM notes WHERE project=? AND id=?", (project, identifier)
    ).fetchone()
    if row is None:
        raise KeyError("Note not found")
    return dict(row)


def note_text(args, *, require_text=False, limit=40_000):
    title = args.get("title", "")
    body = args["body"]
    if (
        not isinstance(title, str) or not isinstance(body, str)
        or len(title) > 240 or "\n" in title or "\r" in title
    ):
        raise ValueError("Expected Markdown text and an optional single-line title of up to 240 characters")
    if len(body) > limit:
        raise ValueError(f"Markdown notes can contain up to {limit:,} characters")
    if require_text and not (title.strip() or body.strip()):
        raise ValueError("A new note needs Markdown text")
    body = markdown_with_title(body, title)
    return note_title({"body": body}), body


def overview(db):
    projects = listing(db, "SELECT project,active FROM projects ORDER BY project")
    now = time.time()
    for project in projects:
        key = project["project"]
        running = listing(
            db,
            f"""SELECT {TURN_COLUMNS} FROM turns JOIN projects USING(project)
                    WHERE project=? AND active=1 AND status='inProgress'
                    AND started>=since AND updated>=?
                    ORDER BY started,turn,thread""",
            (key, now - OBSERVATION_LEASE),
        )
        project["turns"] = with_observation(running) or journey(db, key, {"limit": 1})
    return projects


def storage_stats(db, project=None):
    db.execute("BEGIN")
    now = time.time()
    cutoff = now - STORAGE_WINDOW_DAYS * 86400
    where = "WHERE project=?" if project else ""
    scope = (project,) if project else ()
    projects = listing(
        db,
        f"""WITH turn_totals AS (
                SELECT project, COUNT(*) AS turn_count, SUM(body_bytes) AS turn_bytes,
                       MAX(started) AS latest_turn_started,
                       COUNT(*) FILTER (WHERE started BETWEEN ? AND ?) AS recent_count,
                       SUM(body_bytes) FILTER (WHERE started BETWEEN ? AND ?) AS recent_bytes
                FROM turns INDEXED BY turn_storage {where} GROUP BY project
            ), note_totals AS (
                SELECT project, COUNT(*) AS note_count, SUM(body_bytes) AS note_bytes
                FROM notes INDEXED BY note_storage {where} GROUP BY project
            )
            SELECT project, COALESCE(turn_count, 0) AS turn_count,
                   COALESCE(turn_bytes, 0) AS turn_bytes, latest_turn_started,
                   COALESCE(note_count, 0) AS note_count,
                   COALESCE(note_bytes, 0) AS note_bytes,
                   COALESCE(recent_count, 0) AS recent_count,
                   COALESCE(recent_bytes, 0) AS recent_bytes
            FROM projects LEFT JOIN turn_totals USING (project)
                          LEFT JOIN note_totals USING (project)
            {where} ORDER BY project""",
        (cutoff, now, cutoff, now, *scope, *scope, *scope),
    )
    if project and not projects:
        raise KeyError("Project not found")
    largest = listing(
        db,
        f"""WITH largest AS (
                SELECT project, thread, turn, started, body_bytes
                FROM turns INDEXED BY turn_storage {where}
                ORDER BY body_bytes DESC, started DESC, project, thread, turn LIMIT 10
            )
            SELECT largest.*, substr(turns.summary, 1, 160) AS summary
            FROM largest JOIN turns USING (project, thread, turn)
            ORDER BY largest.body_bytes DESC, largest.started DESC,
                     largest.project, largest.thread, largest.turn""",
        scope,
    )
    recent = {
        "days": STORAGE_WINDOW_DAYS,
        "turn_count": sum(row.pop("recent_count") for row in projects),
        "turn_bytes": sum(row.pop("recent_bytes") for row in projects),
    }
    result = {"recent": recent, "projects": projects, "largest_turns": largest}
    if not project:
        path = db.execute("PRAGMA database_list").fetchone()["file"]
        result.update(database_bytes=0, wal_bytes=0)
        if path:
            result["database_bytes"] = Path(path).stat().st_size
            try:
                result["wal_bytes"] = Path(path + "-wal").stat().st_size
            except FileNotFoundError:
                pass
    return result


def endpoint(db, method, path, args):
    if method == "GET" and path == "projects":
        return listing(db, "SELECT * FROM projects ORDER BY project")
    if method == "GET" and path == "overview":
        return overview(db)
    if method == "GET" and path == "stats" and "project" not in args:
        return storage_stats(db)
    project = args.get("project", "")
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ValueError("Invalid project")
    if method == "GET" and path == "stats":
        return storage_stats(db, project)
    if method == "POST" and path == "register":
        db.execute(
            """INSERT INTO projects VALUES(?,?,1) ON CONFLICT(project) DO UPDATE
                      SET active=1,since=excluded.since WHERE projects.active=0""",
            (project, time.time()),
        )
        return {"project": project}
    if method == "POST" and path == "disable":
        db.execute("UPDATE projects SET active=0 WHERE project=?", (project,))
        return {"project": project}
    if method == "POST" and path == "observe-turn":
        key = project, args["thread"], args["turn"]
        row = db.execute(
            """SELECT started,since FROM turns JOIN projects USING(project)
               WHERE project=? AND thread=? AND turn=? AND active=1 AND status='inProgress'""",
            key,
        ).fetchone()
        if not row or turn_start(args["turn"], row["started"]) < row["since"]:
            return {"accepted": False}
        db.execute(
            "UPDATE turns SET updated=? WHERE project=? AND thread=? AND turn=?",
            (time.time(), *key),
        )
        return {"accepted": True}
    if method == "GET" and path == "journey":
        return journey(db, project, args)
    if method == "GET" and path == "sessions":
        return [
            row["thread"] for row in db.execute(
                "SELECT DISTINCT thread FROM turns WHERE project=? ORDER BY thread", (project,)
            )
        ]
    if method == "GET" and path == "board":
        with db:
            db.execute("BEGIN")
            return board_page(db, project, args)
    if method == "GET" and path == "note":
        return read_note(db, project, args["id"])
    if method == "GET" and path == "transcript":
        row = db.execute(
            "SELECT items FROM turns WHERE project=? AND thread=? AND turn=?",
            (project, args["thread"], args["turn"]),
        ).fetchone()
        if not row:
            raise KeyError("Turn not found")
        return json.loads(row["items"])
    if path == "notes" and method == "GET":
        sql = f"SELECT {NOTE_COLUMNS} FROM notes WHERE project=?"
        values = [project]
        axis = args.get("axis")
        if axis == "project":
            sql += " AND turn IS NULL"
        elif axis == "turn":
            sql += " AND turn IS NOT NULL"
            if args.get("thread"):
                sql += " AND thread=?"
                values.append(args["thread"])
            if args.get("exclude_turn"):
                sql += " AND turn<>?"
                values.append(args["exclude_turn"])
        elif axis:
            raise ValueError("Invalid note axis")
        sql += " ORDER BY created DESC,id DESC"
        if args.get("limit"):
            sql += " LIMIT ?"
            values.append(max(1, min(200, int(args["limit"]))))
        return listing(db, sql, values)[::-1]
    if path == "notes" and method == "POST":
        markdown = args.get("format") == "markdown"
        title, body = note_text(args, require_text=not markdown)
        priority = note_priority(args.get("priority", "normal"))
        thread, turn = args.get("thread") or None, args.get("turn") or None
        if bool(thread) != bool(turn):
            raise ValueError("A note needs a valid target")
        db.execute(
            """INSERT INTO notes(id,project,thread,turn,title,body,created,priority) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (args["id"], project, thread, turn, title, body, time.time(), priority),
        )
        saved = db.execute(
            "SELECT project,thread,turn,version,title,body,priority FROM notes WHERE id=?", (args["id"],)
        ).fetchone()
        if tuple(saved)[:3] != (project, thread, turn):
            raise ValueError("Note ID already belongs to a different target")
        result = {"id": args["id"]}
        if "title" in args or markdown or "priority" in args:
            result.update(
                version=saved["version"],
                matches=(saved["title"], saved["body"]) == (title, body)
                and ("priority" not in args or saved["priority"] == priority),
            )
        return result
    if path == "edit-note" and method == "POST":
        row = read_note(db, project, args["id"])
        title, body = note_text(args, limit=max(40_000, len(row["body"])))
        if row["body"] == body and row["title"] == title:
            return {"id": args["id"], "version": row["version"]}
        if row["version"] != args["version"]:
            raise Conflict(
                "This note changed elsewhere. Reopen it to edit the latest version."
            )
        db.execute(
            "UPDATE notes SET title=?,body=?,version=version+1 WHERE project=? AND id=?",
            (title, body, project, args["id"]),
        )
        return {"id": args["id"], "version": row["version"] + 1}
    if path == "set-note-priority" and method == "POST":
        row = read_note(db, project, args["id"])
        priority = note_priority(args["priority"])
        if row["priority"] == priority:
            return row
        if row["version"] != args["version"]:
            raise Conflict("This note changed elsewhere. Reopen it before changing its status.")
        db.execute(
            "UPDATE notes SET priority=?,version=version+1 WHERE project=? AND id=?",
            (priority, project, row["id"]),
        )
        return row | {"priority": priority, "version": row["version"] + 1}
    if path == "delete-note" and method == "POST":
        row = db.execute(
            "SELECT version FROM notes WHERE project=? AND id=?", (project, args["id"])
        ).fetchone()
        if row:
            if row["version"] != args["version"]:
                raise Conflict("This note changed elsewhere. Reopen it before deleting it.")
            db.execute("DELETE FROM notes WHERE project=? AND id=?", (project, args["id"]))
        return {"id": args["id"], "deleted": True}
    if path == "move-note" and method == "POST":
        row = read_note(db, project, args["id"])
        if not row["turn"]:
            raise ValueError("Only turn notes can be moved")
        target = args["thread"], args["turn"]
        current = row["thread"], row["turn"]
        version = args["version"]
        if current == target and row["version"] in (version, version + 1):
            return row
        if row["version"] != version:
            raise Conflict("This note changed elsewhere. Reopen it before moving it.")
        if not db.execute(
            "SELECT 1 FROM turns WHERE project=? AND thread=? AND turn=?", (project, *target)
        ).fetchone():
            raise KeyError("Target turn not found")
        db.execute(
            "UPDATE notes SET thread=?,turn=?,version=version+1 WHERE project=? AND id=?",
            (*target, project, row["id"]),
        )
        return row | {"thread": target[0], "turn": target[1], "version": version + 1}
    if path == "turns" and method == "POST":
        row = db.execute(
            "SELECT since, active FROM projects WHERE project=?", (project,)
        ).fetchone()
        if (
            not row
            or not row["active"]
            or turn_start(args["turn"], args["started"]) < row["since"]
        ):
            return {"accepted": False}
        if args["status"] not in ("inProgress", "completed", "failed", "interrupted"):
            raise ValueError("Invalid turn status")
        db.execute(
            """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,parent,items,revision,updated,parent_turn)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(project,thread,turn) DO UPDATE SET
                    status=excluded.status,prompt=excluded.prompt,summary=excluded.summary,
                    started=excluded.started,parent=excluded.parent,parent_turn=excluded.parent_turn,
                    items=excluded.items,revision=excluded.revision,
                    updated=COALESCE(excluded.updated,turns.updated)
                    WHERE excluded.revision > turns.revision AND
                    (turns.status='inProgress' OR excluded.status!='inProgress')""",
            (
                project,
                args["thread"],
                args["turn"],
                turn_start(args["turn"], args["started"]),
                args["status"],
                args["prompt"],
                args["summary"],
                args.get("parent"),
                json.dumps(args["items"], ensure_ascii=False),
                args["revision"],
                time.time() if args.get("observed", True) else None,
                args.get("parent_turn"),
            ),
        )
        return {"accepted": True}
    raise KeyError("Unknown endpoint")


class Conflict(Exception):
    pass


class JournalHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        pass

    def reply(self, status, data, mime="application/json", headers=None):
        if mime == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        url = urlsplit(self.path)
        if self.command == "GET" and url.path == "/internal/deployment":
            return self.deployment_probe()
        if self.command == "GET" and url.path == "/healthz":
            try:
                with closing(connect(self.server.database)) as db:
                    db.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
            except sqlite3.Error:
                return self.reply(503, {"status": "unavailable"})
            return self.reply(
                200,
                {"status": "ready", "deployment": self.server.deployment_id},
                headers={SERVER_VERSION_HEADER: SERVER_VERSION},
            )
        if self.server.maintenance_file and self.server.maintenance_file.exists():
            return self.reply(
                503,
                {"error": "Journal maintenance in progress"},
                headers={"Retry-After": "15"},
            )
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/notes.js": ("notes.js", "text/javascript; charset=utf-8"),
            "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css"),
        }
        if self.command == "GET" and url.path in files:
            name, mime = files[url.path]
            return self.reply(200, (STATIC / name).read_bytes(), mime)
        token = self.server.token
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        cookie_name = self.server.browser_cookie
        session = hmac.new(
            token.encode(), b"journal-browser-v1", hashlib.sha256
        ).hexdigest()
        header = self.headers.get("Authorization", "")
        login = url.path == "/api/login" and self.command == "POST"
        authenticated = secrets.compare_digest(
            header.encode(), ("Bearer " + token).encode()
        ) or (
            cookie_name in cookie
            and secrets.compare_digest(
                cookie[cookie_name].value.encode(), session.encode()
            )
        )
        if not login and not authenticated:
            return self.reply(401, {"error": "Authentication required"})
        if not url.path.startswith("/api/"):
            return self.reply(404, {})
        body = {}
        if self.command == "POST":
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"error": "Expected JSON"})
            length = int(self.headers.get("Content-Length", "0"))
            limit = 4096 if login else 64 * 1024 * 1024
            if not 0 < length <= limit:
                return self.reply(413, {"error": "Invalid body length"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
        if login:
            if not secrets.compare_digest(
                str(body.get("token", "")).encode(), token.encode()
            ):
                return self.reply(401, {"error": "Invalid token"})
            secure = (
                "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            )
            return self.reply(
                200,
                {},
                headers={
                    "Set-Cookie": f"{cookie_name}={session}; HttpOnly; SameSite=Strict; Path=/{secure}"
                },
            )
        args = {k: v[0] for k, v in parse_qs(url.query).items()} | body
        with closing(connect(self.server.database)) as db, db:
            if self.command == "POST":
                db.execute("BEGIN IMMEDIATE")
            result = endpoint(db, self.command, url.path[5:], args)
        self.reply(200, result)

    def deployment_probe(self):
        probe_key = self.server.deployment_probe_key
        if not probe_key or not ipaddress.ip_address(self.client_address[0]).is_loopback:
            return self.reply(404, {})
        supplied_key = self.headers.get("X-Journal-Deployment-Key", "")
        authorization = self.headers.get("Authorization", "")
        if not (
            secrets.compare_digest(supplied_key.encode("utf-8"), probe_key.encode("utf-8"))
            and secrets.compare_digest(
                authorization.encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")
            )
        ):
            return self.reply(401, {"error": "Authentication required"})
        try:
            with closing(connect(self.server.database)) as db:
                db.execute("PRAGMA query_only=ON")
                endpoint(db, "GET", "overview", {})
                db.execute("SELECT title, body, version, priority FROM notes LIMIT 1").fetchone()
        except sqlite3.Error:
            return self.reply(503, {"status": "unavailable"},
                              headers={"X-Journal-Deployment-Probe": "1"})
        return self.reply(200, {"status": "ready", "deployment": self.server.deployment_id},
                          headers={"X-Journal-Deployment-Probe": "1"})

    def do_GET(self):
        try:
            self.dispatch()
        except (ValueError, TypeError, AttributeError, sqlite3.IntegrityError) as e:
            self.reply(400, {"error": str(e)})
        except KeyError as e:
            self.reply(404, {"error": str(e)})
        except Conflict as e:
            self.reply(409, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    do_POST = do_GET


def make_server(
    host, port, database, token, *, deployment_id=None, maintenance_file=None,
    deployment_probe_key=None,
):
    if not token:
        raise ValueError("Set AGENT_JOURNAL_TOKEN first")
    with closing(connect(database)) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            for table, field, definition in (
                ("turns", "updated", "REAL"),
                ("turns", "parent_turn", "TEXT"),
                ("notes", "version", "INTEGER NOT NULL DEFAULT 1"),
                ("notes", "title", "TEXT NOT NULL DEFAULT ''"),
                (
                    "notes", "priority",
                    "TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('highlighted','normal','low'))",
                ),
                (
                    "turns",
                    "body_bytes",
                    "INTEGER AS (length(CAST(items AS BLOB)) + length(CAST(prompt AS BLOB))"
                    " + length(CAST(summary AS BLOB))) VIRTUAL",
                ),
                ("notes", "body_bytes", "INTEGER AS (length(CAST(body AS BLOB))) VIRTUAL"),
            ):
                if field not in {
                    row["name"] for row in db.execute(f"PRAGMA table_xinfo({table})")
                }:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS active_turns ON turns(project,updated) WHERE status='inProgress'"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS turn_storage "
                "ON turns(project, body_bytes DESC, started DESC, thread, turn)"
            )
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("DROP INDEX IF EXISTS note_storage")
                for row in db.execute("SELECT id,title,body FROM notes"):
                    body = markdown_with_title(row["body"], row["title"])
                    title = note_title({"body": body})
                    if (title, body) != (row["title"], row["body"]):
                        db.execute(
                            "UPDATE notes SET title=?,body=? WHERE id=?",
                            (title, body, row["id"]),
                        )
                db.execute("PRAGMA user_version=1")
            db.execute("CREATE INDEX IF NOT EXISTS note_storage ON notes(project, body_bytes)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS notes_by_target ON notes(project,thread,turn)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS project_notes_order "
                "ON notes(project,created,id) WHERE turn IS NULL"
            )
    server = ThreadingHTTPServer((host, port), JournalHandler)
    server.token, server.database = token, database
    server.deployment_probe_key = deployment_probe_key
    server.browser_cookie = "journal"
    server.deployment_id, server.maintenance_file = deployment_id, maintenance_file
    return server
