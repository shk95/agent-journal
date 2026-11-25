import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, closing
from fcntl import LOCK_UN, flock
from itertools import chain, pairwise

from websockets.exceptions import WebSocketException
from websockets.sync.client import unix_connect

from .codex_integration import Lifecycle, collector_lock, observer_path
from .common import OBSERVATION_REFRESH, codex_home, local_repo, repo_key, turn_start
from .summary import evidence, worker


class Codex:
    def __init__(self, path=None):
        path = path or codex_home() / "app-server-control/app-server-control.sock"
        self.ws = unix_connect(
            str(path),
            open_timeout=3,
            close_timeout=1,
            max_size=64 * 1024 * 1024,
            proxy=None,
        )
        self.seq = 0
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "agent_journal", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.ws.send(json.dumps({"method": "initialized"}))

    def close(self):
        self.ws.close()

    def call(self, method, params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        deadline = time.monotonic() + 10
        while True:
            reply = json.loads(
                self.ws.recv(timeout=max(0, deadline - time.monotonic()))
            )
            if reply.get("id") != self.seq:
                continue
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']['message']}")
            return reply["result"]

    def pages(self, method, params):
        cursor = None
        while True:
            result = self.call(method, params | ({"cursor": cursor} if cursor else {}))
            yield result["data"]
            cursor = result.get("nextCursor")
            if not cursor:
                break


def short(value, limit=120):
    text = re.sub(r"\s+", " ", value).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def request_text(items):
    messages = [
        " ".join(
            part.get("text", "")
            for part in item.get("content", [])
            if part.get("type") == "text"
        )
        for item in items
        if item["type"] == "userMessage"
    ]
    return " ".join(m for m in messages if m).strip()


def summarize(items, status):
    for item in reversed(items):
        kind = item["type"]
        if kind == "agentMessage" and item.get("text", "").strip():
            return short(item["text"].strip().lstrip("#* "), 160)
        if kind == "commandExecution" and status == "inProgress":
            return short(item.get("command") or "Running a command")
        if kind == "mcpToolCall" and status == "inProgress":
            return short(f"{item.get('server', '')} · {item.get('tool', '')}")
    return {
        "inProgress": "Request received",
        "completed": "Turn completed",
        "interrupted": "Turn interrupted",
        "failed": "Turn failed",
    }.get(status, status)


class Cache:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.scanned = {}
        self.heartbeats = {}
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS registry(project TEXT PRIMARY KEY,since INTEGER);
            CREATE TABLE IF NOT EXISTS summaries(
                project TEXT, thread TEXT, turn TEXT, digest TEXT, record TEXT, context TEXT, ready REAL,
                PRIMARY KEY(project,thread,turn)
            );
            CREATE TABLE IF NOT EXISTS snapshots(
                project TEXT, thread TEXT, turn TEXT, digest TEXT, revision INTEGER, pending TEXT,
                PRIMARY KEY(project,thread,turn)
            );
        """)

    def refresh(self, rows):
        with self.db:
            self.db.execute("DELETE FROM registry")
            self.db.executemany(
                "INSERT INTO registry VALUES(?,?)",
                [(r["project"], r["since"]) for r in rows if r["active"]],
            )
            self.db.execute(
                "DELETE FROM snapshots WHERE project NOT IN (SELECT project FROM registry)"
            )

            self.db.execute(
                "DELETE FROM summaries WHERE project NOT IN (SELECT project FROM registry)"
            )

    def put(self, record, previous=None):
        data = json.dumps(record, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(data.encode()).hexdigest()
        key = tuple(record[k] for k in ("project", "thread", "turn"))
        if record["status"] == "inProgress":
            self.heartbeats.setdefault(key, 0)
        else:
            self.heartbeats.pop(key, None)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT digest,revision FROM snapshots WHERE project=? AND thread=? AND turn=?",
                key,
            ).fetchone()
            if row and digest == row[0]:
                return
            revision = max(time.time_ns() // 1000, row[1] + 1 if row else 0)
            body = json.dumps(record | {"revision": revision}, ensure_ascii=False)
            self.db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?)",
                key + (digest, revision, body),
            )
            if record["status"] != "inProgress" or len(record["items"]) > 1:
                self.db.execute(
                    """INSERT INTO summaries VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,thread,turn)
                                 DO UPDATE SET digest=excluded.digest,record=excluded.record,context=excluded.context,
                                 ready=MIN(summaries.ready,excluded.ready)""",
                    (
                        *key,
                        digest,
                        data,
                        json.dumps(evidence(record, previous), ensure_ascii=False),
                        time.time() + (5 if record["status"] == "inProgress" else 0),
                    ),
                )

    def flush(self, api, active=()):
        for project, thread, turn, rev, body in self.db.execute(
            "SELECT project,thread,turn,revision,pending FROM snapshots WHERE pending IS NOT NULL ORDER BY revision"
        ).fetchall():
            api.request("turns", json.loads(body))
            with self.db:
                self.db.execute(
                    """UPDATE snapshots SET pending=NULL
                                   WHERE project=? AND thread=? AND turn=? AND revision=?""",
                    (project, thread, turn, rev),
                )
        now = time.monotonic()
        self.heartbeats = {
            key: sent for key, sent in self.heartbeats.items() if key in active
        }
        for project, thread, turn in active:
            key = (project, thread, turn)
            if now - self.heartbeats.get(key, 0) >= OBSERVATION_REFRESH:
                api.request(
                    "observe-turn", {"project": project, "thread": thread, "turn": turn}
                )
                self.heartbeats[key] = now

    def has_pending_work(self):
        return self.db.execute(
            """SELECT EXISTS(SELECT 1 FROM snapshots WHERE pending IS NOT NULL)
                   OR EXISTS(SELECT 1 FROM summaries WHERE ready <= ?)""",
            (time.time(),),
        ).fetchone()[0]


def scan(rpc, cache):
    active = set()
    registered = dict(cache.db.execute("SELECT project,since FROM registry"))
    if not registered:
        return active
    oldest = min(registered.values())
    for page in rpc.pages(
        "thread/list", {"sortKey": "updated_at", "sortDirection": "desc", "limit": 60}
    ):
        for thread in page:
            if thread["updatedAt"] < int(oldest):
                return active
            origin = (thread.get("gitInfo") or {}).get("originUrl")
            project = (
                repo_key(origin)
                if origin
                else local_repo(thread["cwd"])
                if thread.get("cwd")
                else None
            )
            if project not in registered:
                continue
            try:
                stamp = (thread["updatedAt"], project, registered[project])
                settled = (thread.get("status") or {}).get("type") in (
                    "idle",
                    "notLoaded",
                )
                if settled and cache.scanned.get(thread["id"]) == stamp:
                    continue
                stable = scan_thread(
                    rpc, cache, thread, project, registered[project], active
                )
                if stable and settled and time.time() - thread["updatedAt"] > 5:
                    cache.scanned[thread["id"]] = stamp
            except RuntimeError as e:
                print(f"Thread {thread['id']}: {e}", file=sys.stderr)
    return active


def scan_thread(rpc, cache, thread, project, since, active=None):
    thread_id = thread["id"]
    args = {
        "threadId": thread_id,
        "limit": 16,
        "sortDirection": "desc",
        "itemsView": "full",
    }
    parent = thread.get("forkedFromId")
    inherited = set()
    parent_available = True
    if parent:
        try:
            for page in rpc.pages(
                "thread/turns/list",
                args | {"threadId": parent, "itemsView": "notLoaded", "limit": 100},
            ):
                inherited.update(row["id"] for row in page)
                if any((row.get("startedAt") or 0) < int(since) for row in page):
                    break
        except RuntimeError:
            parent_available = False

    pending = []
    stable = True
    fork_turn = None
    history = (
        row
        for page in rpc.pages("thread/turns/list", args)
        for row in page
        if any(item["type"] == "userMessage" for item in row.get("items", []))
    )
    for turn, prior in pairwise(chain(history, [None])):
        started = turn.get("startedAt")
        if started is None:
            continue
        if started < int(since):
            break
        items = turn.get("items", [])
        if turn["id"] in inherited:
            fork_turn = turn["id"]
            break
        if turn_start(turn["id"], started) < since:
            continue
        if not parent_available and (
            not thread.get("createdAt") or started <= thread["createdAt"]
        ):
            stable = False
            continue
        status = turn["status"]
        if status == "interrupted" and turn.get("completedAt") is None:
            stable = False
            continue
        if status not in ("completed", "failed", "interrupted", "inProgress"):
            continue
        if status == "inProgress" and active is not None:
            active.add((project, thread_id, turn["id"]))
        previous = None
        if prior and turn_start(prior["id"], prior.get("startedAt") or 0) >= since:
            previous = {
                "request": short(request_text(prior.get("items", [])), 400),
                "answer": summarize(prior.get("items", []), prior["status"]),
            }
        record = {
            "project": project,
            "thread": thread_id,
            "turn": turn["id"],
            "parent": parent,
            "started": started,
            "status": status,
            "prompt": request_text(items) or "Request with attachments",
            "summary": summarize(items, status),
            "items": items,
        }
        if parent:
            pending.append((record, previous))
        else:
            cache.put(record, previous)
    for record, previous in pending:
        cache.put(record | {"parent_turn": fork_turn}, previous)
    return stable and parent_available


def collect(api, socket, interval, once, lock_fd=None):
    path = observer_path(api)
    stop = threading.Event()

    def start_summary_worker():
        thread = threading.Thread(target=worker, args=(path, stop), daemon=True)
        thread.start()
        return thread

    with ExitStack() as resources:
        lock = os.fdopen(lock_fd, "a") if lock_fd is not None else collector_lock(path)
        if lock is None:
            raise ValueError("An Agent Journal collector is already running")
        resources.enter_context(lock)
        resources.callback(flock, lock, LOCK_UN)
        cache = Cache(path)
        resources.callback(cache.db.close)
        if not once:
            resources.callback(stop.set)
            summary_thread = start_summary_worker()
        if once or lock_fd is None:
            observe(api, cache, socket, interval, once)
            return

        lifecycle = Lifecycle(path.parent / "codex-sessions")

        def after_cycle(loaded):
            nonlocal summary_thread
            lifecycle.prune(loaded)
            with lifecycle.events():
                if not lifecycle.finished(cache):
                    if stop.is_set() and not summary_thread.is_alive():
                        stop.clear()
                        summary_thread = start_summary_worker()
                    return False
                stop.set()
                if summary_thread.is_alive():
                    return False
                # Release the collector lock before a new session checks it.
                resources.close()
                return True

        observe(api, cache, socket, interval, once, after_cycle)


def observe(api, cache, socket, interval, once, after_cycle=None):
    while True:
        errors = []
        active = set()
        loaded = None
        try:
            cache.refresh(api.request("projects"))
        except (OSError, RuntimeError, ValueError, WebSocketException) as e:
            errors.append(f"Project registry: {e}")
        try:
            with closing(Codex(socket)) as rpc:
                if after_cycle is not None:
                    loaded = set(chain.from_iterable(rpc.pages("thread/loaded/list", {})))
                active = scan(rpc, cache)
        except (OSError, RuntimeError, ValueError, WebSocketException) as e:
            errors.append(f"Codex: {e}")
        try:
            cache.flush(api, active)
        except (OSError, RuntimeError, ValueError, WebSocketException) as e:
            errors.append(f"Sync: {e}")
        if errors:
            if once:
                raise RuntimeError(" / ".join(errors))
            print(" / ".join(errors), file=sys.stderr)
        if once:
            return
        if after_cycle is not None and after_cycle(loaded):
            return
        time.sleep(interval)
