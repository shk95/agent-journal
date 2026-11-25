import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agent_journal.common import OBSERVATION_LEASE, Api, repo_key
from agent_journal.observer import Cache, scan
from agent_journal.server import make_server
from agent_journal.summary import work_once


def turn(key, started, prompt, response="", status="completed"):
    items = [{"type": "userMessage", "content": [{"type": "text", "text": prompt}]}]
    if response:
        items.append({"type": "agentMessage", "text": response})
    return {
        "id": key,
        "startedAt": started,
        "completedAt": None if status == "inProgress" else started + 1,
        "status": status,
        "items": items,
    }


class Fixture:
    def __init__(self, since):
        self.now = since + 1
        self.turns = {
            "a": [
                turn("old", since - 1, "private prior history"),
                turn("one", since, "choose color", "green"),
            ],
            "b": [
                turn("one", since, "choose color", "green"),
                turn("two", since + 1, "yes", "green chosen"),
            ],
        }
        self.projects = {"a": "p", "b": "p", "other": "other"}
        self.read = []

    def pages(self, method, params):
        if method == "thread/list":
            yield [
                {
                    "id": session,
                    "updatedAt": self.now,
                    "gitInfo": {"originUrl": f"git@github.com:test/{repo}.git"},
                    "status": {"type": "active"},
                    "forkedFromId": "a" if session == "b" else None,
                }
                for session, repo in self.projects.items()
            ]
        else:
            key = params["threadId"]
            self.read.append(key)
            yield list(reversed(self.turns[key]))


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.server = make_server("127.0.0.1", 0, self.path / "server", "test-token")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = patch.dict(
            os.environ,
            {
                "AGENT_JOURNAL_URL": f"http://127.0.0.1:{self.server.server_port}",
                "AGENT_JOURNAL_TOKEN": "test-token",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api.request("register", {"project": "test/p"})
        projects = self.api.request("projects")
        self.since = projects[0]["since"]
        self.cache = Cache(self.path / "local")
        self.addCleanup(self.cache.db.close)
        self.cache.refresh(projects)
        self.source = Fixture(self.since)

    def rows(self):
        return self.api.request("journey", project="test/p")

    def test_scope_fork_history_and_note(self):
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        rows = self.rows()
        self.assertEqual([r["turn"] for r in rows], ["one", "two"])
        self.assertNotIn("other", self.source.read)
        self.assertEqual(rows[-1]["parent"], "a")
        self.assertEqual(rows[-1]["parent_turn"], "one")
        self.assertEqual([row["graph"] for row in rows], ["●─┐", "  ●"])
        args = {"project": "test/p", "thread": "a", "turn": "one"}
        self.assertEqual(self.api.request("transcript", **args)[-1]["text"], "green")
        note = args | {"id": "memo", "body": "remember"}
        self.api.request("notes", note)
        self.api.request("notes", note)
        self.assertEqual(len(self.api.request("notes", project="test/p")), 1)
        with self.assertRaises(RuntimeError):
            self.api.request("notes", note | {"turn": "missing", "id": "invalid"})

    def test_retry_and_stale_response(self):
        scan(self.source, self.cache)
        payload = json.loads(
            self.cache.db.execute(
                "SELECT pending FROM snapshots WHERE turn='one'"
            ).fetchone()[0]
        )
        self.api.request("turns", payload)
        reopened = Cache(self.path / "local")
        try:
            reopened.flush(self.api)
        finally:
            reopened.db.close()
        while work_once(self.cache.db, lambda context: "semantic result"):
            pass
        self.cache.flush(self.api)
        self.api.request("turns", payload)
        self.assertEqual(
            [r["summary"] for r in self.rows()], ["semantic result", "semantic result"]
        )

    def test_registration_boundary_and_reenable(self):
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        self.api.request("disable", {"project": "test/p"})
        self.cache.refresh(self.api.request("projects"))
        self.assertEqual(
            self.cache.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0
        )
        self.assertEqual(len(self.rows()), 2)
        with patch("agent_journal.server.time.time", return_value=self.since + 10):
            self.api.request("register", {"project": "test/p"})
        new = self.api.request("projects")
        self.cache.refresh(new)
        scan(self.source, self.cache)
        self.assertEqual(
            self.cache.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0
        )

    def test_transient_interrupt_and_stale_summary(self):
        live = turn("three", self.since + 1, "verify", status="inProgress")
        self.source.turns["a"].append(live)
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        live["status"] = "interrupted"
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        self.assertEqual(
            next(r for r in self.rows() if r["turn"] == "three")["status"], "inProgress"
        )
        live.update(status="completed", completedAt=self.since + 2)
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        self.assertEqual(
            next(r for r in self.rows() if r["turn"] == "three")["status"], "completed"
        )

    def test_slow_summary_cannot_replace_newer_turn(self):
        scan(self.source, self.cache)

        def finish_old_job(context):
            self.source.turns["a"][-1]["items"][-1]["text"] = "revised final"
            scan(self.source, self.cache)
            return "obsolete summary"

        self.assertTrue(work_once(self.cache.db, finish_old_job))
        self.cache.flush(self.api)
        self.assertEqual(self.rows()[0]["summary"], "revised final")
        while work_once(self.cache.db, lambda ctx: "latest summary"):
            pass
        self.cache.flush(self.api)
        self.assertEqual(self.rows()[0]["summary"], "latest summary")

    def test_registration_between_requests_in_one_second(self):
        second = int(self.since) + 5
        self.api.request("disable", {"project": "test/p"})
        with patch("agent_journal.server.time.time", return_value=second + 0.25):
            self.api.request("register", {"project": "test/p"})
        self.cache.refresh(self.api.request("projects"))

        def turn_id(offset):
            return str(
                UUID(int=((second * 1000 + offset) << 80) | (7 << 76) | (2 << 62))
            )

        self.source.now = second
        self.source.turns["a"] = [
            turn(turn_id(100), second, "before registration"),
            turn(turn_id(700), second, "after registration"),
        ]
        self.source.turns["b"] = []
        self.cache.flush(self.api, scan(self.source, self.cache))
        self.assertEqual([r["turn"] for r in self.rows()], [turn_id(700)])

    def test_connection_loss_recovery_and_disable_status(self):
        live = turn("live", self.since + 1, "verify", status="inProgress")
        self.source.turns["a"].append(live)
        project = {"project": "test/p"}

        def latest():
            return next(r for r in self.rows() if r["turn"] == "live")

        self.cache.flush(self.api, scan(self.source, self.cache))
        self.assertEqual(latest()["observation"], "observed")
        with patch(
            "agent_journal.server.time.time",
            return_value=self.since + OBSERVATION_LEASE + 5,
        ):
            self.assertEqual(latest()["observation"], "unavailable")
            self.api.request("observe-turn", project | {"thread": "a", "turn": "live"})
            self.assertEqual(latest()["observation"], "observed")
        self.api.request("disable", project)
        self.assertEqual(latest()["observation"], "stopped")

    def test_invalid_token(self):
        self.api.token = "invalid"
        with self.assertRaisesRegex(RuntimeError, "401"):
            self.api.request("projects")

    def test_journey_pagination_and_overview(self):
        self.cache.flush(self.api, scan(self.source, self.cache))
        rows = self.api.request("journey", project="test/p", limit=1)
        latest = rows[0]
        self.assertEqual(latest["turn"], "two")
        before = json.dumps([latest["started"], latest["turn"], latest["thread"]])
        earlier = self.api.request("journey", project="test/p", limit=1, before=before)
        self.assertEqual([r["turn"] for r in earlier], ["one"])
        self.assertEqual(
            [r["turn"] for r in self.api.request("overview")[0]["turns"]], ["two"]
        )

    def test_edit_note_uses_version_and_preserves_target(self):
        self.cache.flush(self.api, scan(self.source, self.cache))
        project = {"project": "test/p"}
        for key, target in (("attached", {"thread": "a", "turn": "one"}), ("free", {})):
            self.api.request(
                "notes", project | {"id": key, "body": "Original"} | target
            )
            update = project | {"id": key, "body": "Updated", "version": 1}
            self.assertEqual(self.api.request("edit-note", update)["version"], 2)
            self.assertEqual(self.api.request("edit-note", update)["version"], 2)
            with self.assertRaisesRegex(RuntimeError, "409"):
                self.api.request("edit-note", update | {"body": "Conflicting update"})
        notes = self.api.request("notes", **project)
        self.assertEqual(
            [(n["body"], n["turn"]) for n in notes],
            [("Updated", "one"), ("Updated", None)],
        )

    def test_github_keys(self):
        for url in (
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo.git",
            "https://github.com/owner/repo",
        ):
            self.assertEqual(repo_key(url), "owner/repo")
        self.assertIsNone(repo_key("https://github.com.example/owner/repo"))
