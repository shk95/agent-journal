import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import OBSERVATION_LEASE, PROJECT, turn_start

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


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def listing(db, sql, values=()):
    return [dict(row) for row in db.execute(sql, values)]


def journey(db, project):
    rows = listing(
        db,
        """SELECT thread,turn,parent,started,status,prompt,summary,updated,since,active
           FROM turns JOIN projects USING(project) WHERE project=?
           ORDER BY started,turn,thread""",
        (project,),
    )
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
    return sorted(
        rows,
        key=lambda r: (turn_start(r["turn"], r["started"]), r["thread"], r["turn"]),
    )


def endpoint(db, method, path, args):
    if method == "GET" and path == "projects":
        return listing(db, "SELECT * FROM projects ORDER BY project")
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
        return journey(db, project)
    if method == "GET" and path == "transcript":
        row = db.execute(
            "SELECT items FROM turns WHERE project=? AND thread=? AND turn=?",
            (project, args["thread"], args["turn"]),
        ).fetchone()
        if not row:
            raise KeyError("Turn not found")
        return json.loads(row["items"])
    if path == "notes" and method == "GET":
        return listing(
            db, "SELECT * FROM notes WHERE project=? ORDER BY created,id", (project,)
        )
    if path == "notes" and method == "POST":
        body = args["body"].strip()
        thread, turn = args.get("thread"), args.get("turn")
        if bool(thread) != bool(turn) or not body:
            raise ValueError("A note needs text and a valid target")
        db.execute(
            "INSERT INTO notes VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (args["id"], project, thread, turn, body, time.time()),
        )
        return {"id": args["id"]}
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
            """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,parent,items,revision,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(project,thread,turn) DO UPDATE SET
                    status=excluded.status,prompt=excluded.prompt,summary=excluded.summary,
                    parent=excluded.parent,items=excluded.items,revision=excluded.revision,
                    updated=COALESCE(excluded.updated,turns.updated)
                    WHERE excluded.revision > turns.revision AND
                    (turns.status='inProgress' OR excluded.status!='inProgress')""",
            (
                project,
                args["thread"],
                args["turn"],
                args["started"],
                args["status"],
                args["prompt"],
                args["summary"],
                args.get("parent"),
                json.dumps(args["items"], ensure_ascii=False),
                args["revision"],
                time.time() if args.get("observed", True) else None,
            ),
        )
        return {"accepted": True}
    raise KeyError("Unknown endpoint")


class JournalHandler(BaseHTTPRequestHandler):
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
        session = hmac.new(
            token.encode(), b"journal-browser-v1", hashlib.sha256
        ).hexdigest()
        body = {}
        if self.command == "POST":
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"error": "Expected JSON"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64 * 1024 * 1024:
                return self.reply(413, {"error": "Invalid body length"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
        header = self.headers.get("Authorization", "")
        if url.path == "/api/login" and self.command == "POST":
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
                    "Set-Cookie": f"journal={session}; HttpOnly; SameSite=Strict; Path=/{secure}"
                },
            )
        if not (
            secrets.compare_digest(header.encode(), ("Bearer " + token).encode())
            or (
                "journal" in cookie
                and secrets.compare_digest(
                    cookie["journal"].value.encode(), session.encode()
                )
            )
        ):
            return self.reply(401, {"error": "Authentication required"})
        if not url.path.startswith("/api/"):
            return self.reply(404, {})
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
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = do_GET


def make_server(host, port, database, token):
    if not token:
        raise ValueError("Set AGENT_JOURNAL_TOKEN first")
    with closing(connect(database)) as db:
        db.executescript(SCHEMA)
        if "updated" not in {
            row["name"] for row in db.execute("PRAGMA table_info(turns)")
        }:
            db.execute("ALTER TABLE turns ADD COLUMN updated REAL")
    server = ThreadingHTTPServer((host, port), JournalHandler)
    server.token, server.database = token, database
    return server
