import hashlib
import hmac
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
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(project),
    thread TEXT, turn TEXT, body TEXT NOT NULL, created REAL NOT NULL,
    FOREIGN KEY(project,thread,turn) REFERENCES turns(project,thread,turn)
);
"""
STATIC = Path(__file__).parent / "static"
SERVER_VERSION = version("agent-journal")


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


def journey(db, project, args):
    sql = (
        f"SELECT {TURN_COLUMNS} FROM turns JOIN projects USING(project) WHERE project=?"
    )
    values = [project]
    if args.get("thread"):
        sql += " AND thread=?"
        values.append(args["thread"])
    if args.get("exclude_turn"):
        sql += " AND turn<>?"
        values.append(args["exclude_turn"])
    if args.get("before"):
        cursor = json.loads(args["before"])
        if not isinstance(cursor, list) or len(cursor) != 3:
            raise ValueError("Invalid journey cursor")
        sql += " AND (started,turn,thread)<(?,?,?)"
        values.extend(cursor)
    sql += " ORDER BY started DESC,turn DESC,thread DESC"
    if args.get("limit"):
        sql += " LIMIT ?"
        values.append(max(1, min(200, int(args["limit"]))))
    rows = with_observation(listing(db, sql, values)[::-1])
    if args.get("lanes") == "true":
        for row, lanes in zip(rows, layout(rows)):
            row["graph_lanes"] = lanes
    else:
        for row, line in zip(rows, graph(rows)):
            row["graph"] = line
    return rows


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


def endpoint(db, method, path, args):
    if method == "GET" and path == "projects":
        return listing(db, "SELECT * FROM projects ORDER BY project")
    if method == "GET" and path == "overview":
        return overview(db)
    project = args.get("project", "")
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ValueError("Invalid project")
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
    if method == "GET" and path == "transcript":
        row = db.execute(
            "SELECT items FROM turns WHERE project=? AND thread=? AND turn=?",
            (project, args["thread"], args["turn"]),
        ).fetchone()
        if not row:
            raise KeyError("Turn not found")
        return json.loads(row["items"])
    if path == "notes" and method == "GET":
        sql, values = "SELECT * FROM notes WHERE project=?", [project]
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
        body = args["body"].strip()
        thread, turn = args.get("thread") or None, args.get("turn") or None
        if bool(thread) != bool(turn) or not body or len(body) > 40_000:
            raise ValueError("A note needs text and a valid target")
        db.execute(
            """INSERT INTO notes(id,project,thread,turn,body,created) VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (args["id"], project, thread, turn, body, time.time()),
        )
        saved = db.execute(
            "SELECT project,thread,turn FROM notes WHERE id=?", (args["id"],)
        ).fetchone()
        if tuple(saved) != (project, thread, turn):
            raise ValueError("Note ID already belongs to a different target")
        return {"id": args["id"]}
    if path == "edit-note" and method == "POST":
        body = args["body"].strip()
        if not body or len(body) > 40_000:
            raise ValueError("A note needs text")
        row = db.execute(
            "SELECT body,version FROM notes WHERE project=? AND id=?",
            (project, args["id"]),
        ).fetchone()
        if not row:
            raise KeyError("Note not found")
        if row["body"] == body:
            return {"id": args["id"], "version": row["version"]}
        if row["version"] != args["version"]:
            raise Conflict(
                "This note changed elsewhere. Reopen it to edit the latest version."
            )
        db.execute(
            "UPDATE notes SET body=?,version=version+1 WHERE project=? AND id=?",
            (body, project, args["id"]),
        )
        return {"id": args["id"], "version": row["version"] + 1}
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
            "/app.js": ("app.js", "text/javascript"),
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
    host, port, database, token, *, deployment_id=None, maintenance_file=None
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
            ):
                if field not in {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})")
                }:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS active_turns ON turns(project,updated) WHERE status='inProgress'"
            )
    server = ThreadingHTTPServer((host, port), JournalHandler)
    server.token, server.database = token, database
    server.browser_cookie = "journal"
    server.deployment_id, server.maintenance_file = deployment_id, maintenance_file
    return server
