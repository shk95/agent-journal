"""Exercise two independent observers against a single local server."""

import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from websockets.sync.server import unix_serve

from agent_journal.backup import backup
from agent_journal.common import Api
from agent_journal.mcp_server import locate_request
from agent_journal.observer import Cache, observe
from agent_journal.server import make_server

PROJECT = "example/journal-fixture"


class CodexFixture:
    def __init__(self, path, thread, turns):
        self.thread, self.turns = thread, turns
        self.server = unix_serve(self.client, str(path))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def close(self):
        self.server.shutdown()
        self.server_thread.join(timeout=5)
        if self.server_thread.is_alive():
            raise RuntimeError("The Codex fixture did not stop")

    def client(self, websocket):
        for raw in websocket:
            request = json.loads(raw)
            if "id" not in request:
                continue
            method = request["method"]
            if method == "thread/list":
                result = {"data": [self.thread]}
            elif method == "thread/turns/list":
                result = {"data": self.turns[::-1]}
            else:
                if method != "initialize":
                    raise AssertionError(method)
                result = {}
            websocket.send(json.dumps({"id": request["id"], "result": result}))


def turn(turn_id, started, prompt, status, answer=None):
    items = [{"type": "userMessage", "content": [{"type": "text", "text": prompt}]}]
    if answer:
        items.append({"type": "agentMessage", "text": answer})
    return {
        "id": turn_id,
        "startedAt": started,
        "status": status,
        "completedAt": None if status == "inProgress" else started + 1,
        "items": items,
    }


class LocalStackTest(unittest.TestCase):
    def test_two_observers_disconnect_and_backup_restore(self):
        with tempfile.TemporaryDirectory(prefix="journal-stack-") as tmp:
            root = Path(tmp)
            source = root / "source.sqlite3"
            destination = root / "backup.sqlite3"
            server = self.serve(source)
            with patch.dict(
                os.environ,
                {
                    "AGENT_JOURNAL_HOME": tmp,
                    "AGENT_JOURNAL_TOKEN": "fixture-access",
                    "AGENT_JOURNAL_URL": f"http://127.0.0.1:{server.server_port}",
                },
            ):
                api = Api()
                api.request("register", {"project": PROJECT})
                since = int(api.request("projects")[0]["since"]) + 1
                self.exercise(root, api, since)
                backup(source, destination)
                restored = self.serve(destination)
                copy = Api(f"http://127.0.0.1:{restored.server_port}", "fixture-access")
                self.assertEqual(
                    copy.request("journey", project=PROJECT),
                    api.request("journey", project=PROJECT),
                )
                self.assertEqual(
                    copy.request("notes", project=PROJECT),
                    api.request("notes", project=PROJECT),
                )

    def serve(self, path):
        server = make_server("127.0.0.1", 0, path, "fixture-access")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def exercise(self, root, api, since):
        metadata = {
            "updatedAt": since + 2,
            "status": {"type": "active"},
            "gitInfo": {"originUrl": f"git@github.com:{PROJECT}.git"},
        }
        local = CodexFixture(
            root / "local.sock",
            metadata | {"id": "local"},
            [
                turn("first", since, "Build the example", "completed", "Built"),
                turn("second", since + 2, "Note the previous result", "inProgress"),
            ],
        )
        remote = CodexFixture(
            root / "remote.sock",
            metadata | {"id": "remote"},
            [turn("other", since + 1, "Check the example", "completed", "Passed")],
        )
        try:
            for name in ("local", "remote"):
                cache = Cache(root / f"{name}.sqlite3")
                with closing(cache.db):
                    observe(api, cache, root / f"{name}.sock", 0, True)
            self.assertEqual(
                locate_request(
                    PROJECT, "Note the previous result", root / "local.sock"
                ),
                ("local", "second", since + 2),
            )
            self.assertEqual(
                [row["turn"] for row in api.request("journey", project=PROJECT)],
                ["first", "other", "second"],
            )

            local.turns[1].update(status="completed", completedAt=since + 3)
            cache = Cache(root / "local.sqlite3")
            try:
                with patch.object(
                    api, "request", side_effect=OSError("simulated outage")
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated outage"):
                        observe(api, cache, root / "local.sock", 0, True)
            finally:
                cache.db.close()
            cache = Cache(root / "local.sqlite3")
            with closing(cache.db):
                observe(api, cache, root / "local.sock", 0, True)
                observe(api, cache, root / "local.sock", 0, True)
            rows = api.request("journey", project=PROJECT)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["status"], "completed")
            api.request(
                "notes",
                {
                    "project": PROJECT,
                    "id": "note",
                    "thread": "local",
                    "turn": "first",
                    "body": "Keep this result.",
                },
            )
        finally:
            for fixture in (local, remote):
                fixture.close()
