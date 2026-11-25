import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agent_journal.cli import main
from agent_journal.common import OBSERVATION_LEASE, Api, repo_key
from agent_journal.mcp_server import make_mcp
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
                "AGENT_JOURNAL_HOME": str(self.path),
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

    def test_web_command_opens_configured_journal(self):
        for options, fragment in (
            ([], ""),
            (["--project", "Test/P"], "#test%2Fp"),
        ):
            with (
                self.subTest(options=options),
                patch.object(sys, "argv", ["agent-journal", "web", *options]),
                patch("webbrowser.open_new_tab") as open_tab,
                patch("builtins.print") as print_url,
            ):
                main()
                url = self.api.base + "/" + fragment
                open_tab.assert_called_once_with(url)
                print_url.assert_called_once_with(url, flush=True)

    def test_first_web_run_connects_before_opening_the_browser(self):
        saved = self.path / "connection.json"
        with (
            patch.object(sys, "argv", ["agent-journal", "web"]),
            patch.dict(
                os.environ, {"AGENT_JOURNAL_URL": "", "AGENT_JOURNAL_TOKEN": ""}
            ),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys.stdout, "isatty", return_value=True),
            patch("builtins.input", return_value=self.api.base) as url_prompt,
            patch("getpass.getpass", side_effect=["wrong-token", self.api.token]),
            patch("builtins.print"),
            patch("webbrowser.open_new_tab") as open_tab,
        ):
            with self.assertRaises(SystemExit):
                main()
            self.assertFalse(saved.exists())
            open_tab.assert_not_called()
            main()
            self.assertTrue(saved.exists())
            open_tab.assert_called_once_with(self.api.base + "/")
            main()
            self.assertEqual(open_tab.call_count, 2)
            self.assertEqual(url_prompt.call_count, 2)

    def test_tui_command_can_disable_mouse(self):
        with (
            patch.object(
                sys,
                "argv",
                ["agent-journal", "tui", "--project", "Test/P", "--no-mouse"],
            ),
            patch("agent_journal.tui.run") as run,
        ):
            main()
        self.assertEqual(run.call_args.args[1], "test/p")
        self.assertEqual(run.call_args.kwargs, {"mouse": False})

    def test_default_tui_exits_outside_a_github_checkout(self):
        with (
            patch.object(sys, "argv", ["agent-journal"]),
            patch("agent_journal.cli.local_repo", return_value=None),
            patch("agent_journal.cli.Api") as api,
            patch("builtins.print") as report,
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("No GitHub project found", report.call_args.args[0])
        api.assert_not_called()

    def test_first_tui_run_connects_and_saves_private_settings(self):
        with (
            patch.object(sys, "argv", ["agent-journal"]),
            patch.dict(
                os.environ, {"AGENT_JOURNAL_URL": "", "AGENT_JOURNAL_TOKEN": ""}
            ),
            patch("agent_journal.cli.local_repo", return_value="test/p"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys.stdout, "isatty", return_value=True),
            patch("builtins.input", return_value=self.api.base) as url_prompt,
            patch("getpass.getpass", return_value=self.api.token) as token_prompt,
            patch("builtins.print"),
            patch("agent_journal.tui.run") as run,
        ):
            main()
            main()
        url_prompt.assert_called_once()
        token_prompt.assert_called_once()
        self.assertEqual(run.call_count, 2)
        saved = self.path / "connection.json"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(saved.read_text()),
            {"url": self.api.base, "token": self.api.token},
        )

    def test_tui_exits_with_registration_notice_for_an_unknown_project(self):
        with (
            patch.object(sys, "argv", ["agent-journal"]),
            patch("agent_journal.cli.local_repo", return_value="test/unregistered"),
            patch("builtins.print") as report,
            patch("agent_journal.tui.run") as run,
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Ask Codex to register", report.call_args.args[0])
        run.assert_not_called()

    def test_scope_fork_history_and_note(self):
        scan(self.source, self.cache)
        self.cache.flush(self.api)
        rows = self.rows()
        self.assertEqual([r["turn"] for r in rows], ["one", "two"])
        self.assertNotIn("other", self.source.read)
        self.assertEqual(rows[-1]["parent"], "a")
        self.assertEqual(rows[-1]["parent_turn"], "one")
        self.assertEqual([row["graph"] for row in rows], ["●─┐", "  ●"])
        browser_rows = self.api.request("journey", project="test/p", lanes="true")
        self.assertEqual(
            [row["graph_lanes"] for row in browser_rows],
            [
                {
                    "lane": 0,
                    "tracks": [[0, "a", False, False], [1, "b", False, True]],
                    "forks": [1],
                },
                {"lane": 1, "tracks": [[1, "b", True, False]], "forks": []},
            ],
        )
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

    def test_mcp_scoped_read_and_explicit_note(self):
        self.cache.flush(self.api, scan(self.source, self.cache))

        async def check():
            params = StdioServerParameters(
                command=str(Path(sys.executable).with_name("agent-journal")),
                args=["mcp", "--project", "test/p"],
                env=dict(os.environ),
            )
            async with Client(params, raise_exceptions=True) as client:
                response = await client.call_tool("read_journal", {})
                value = response.structured_content
                self.assertEqual(value["axes"], ["journey"])
                self.assertNotIn("project_notes", value)
                args = {
                    "request_id": "mcp-note",
                    "body": "Remember this",
                    "thread": "a",
                    "turn": "one",
                }
                for _ in range(2):
                    self.assertFalse(
                        (await client.call_tool("add_note", args)).is_error
                    )
                args = {"axes": ["turn_notes", "project_notes"], "thread": "a"}
                value = (
                    await client.call_tool("read_journal", args)
                ).structured_content
                self.assertNotIn("journey", value)
                self.assertEqual(value["project_notes"], [])
                self.assertEqual([n["turn"] for n in value["turn_notes"]], ["one"])

        asyncio.run(check())

    def test_mcp_can_start_before_the_machine_is_connected(self):
        async def check():
            params = StdioServerParameters(
                command=str(Path(sys.executable).with_name("agent-journal")),
                args=["mcp"],
                cwd=self.path,
                env=dict(os.environ)
                | {"AGENT_JOURNAL_TOKEN": "", "AGENT_JOURNAL_URL": ""},
            )
            async with Client(params, raise_exceptions=False) as client:
                self.assertIn(
                    "register_project",
                    [tool.name for tool in (await client.list_tools()).tools],
                )
                result = await client.call_tool("journal_projects", {})
                self.assertTrue(result.is_error)
                self.assertIn("agent-journal connect", result.content[0].text)
                result = await client.call_tool("register_project", {})
                self.assertTrue(result.is_error)
                self.assertIn("agent-journal connect", result.content[0].text)
                self.api.save()
                self.assertFalse(
                    (await client.call_tool("journal_projects", {})).is_error
                )

        asyncio.run(check())

    def test_mcp_registration_checks_github_and_keeps_active_cutoff(self):
        async def check():
            success = subprocess.CompletedProcess([], 0, "Test/New\n", "")
            failure = subprocess.CompletedProcess([], 1, "", "Repository not found")
            with (
                patch("agent_journal.mcp_server.local_repo", return_value="test/new"),
                patch(
                    "agent_journal.mcp_server.subprocess.run",
                    side_effect=[success, success, failure],
                ) as gh,
            ):
                rejected_journal = Api(self.api.base, "wrong-token")
                async with Client(
                    make_mcp(rejected_journal), raise_exceptions=False
                ) as client:
                    result = await client.call_tool("register_project", {})
                    self.assertTrue(result.is_error)
                    self.assertIn("HTTP 401", result.content[0].text)
                    gh.assert_not_called()
                async with Client(make_mcp(self.api), raise_exceptions=False) as client:
                    for attempt in range(2):
                        result = await client.call_tool("register_project", {})
                        self.assertFalse(result.is_error)
                        self.assertEqual(
                            result.structured_content, {"project": "test/new"}
                        )
                        row = next(
                            p
                            for p in self.api.request("projects")
                            if p["project"] == "test/new"
                        )
                        if attempt == 0:
                            cutoff = row["since"]
                        self.assertEqual(row["since"], cutoff)
                        self.assertTrue(row["active"])
                    rejected = await client.call_tool(
                        "register_project", {"project": "Test/Missing"}
                    )
                    self.assertTrue(rejected.is_error)
                    self.assertIn("Repository not found", rejected.content[0].text)
                self.assertEqual(
                    gh.call_args_list[0].args[0][-3:],
                    ["repos/test/new", "--jq", ".full_name"],
                )
            self.assertNotIn(
                "test/missing", [row["project"] for row in self.api.request("projects")]
            )

        asyncio.run(check())

    def test_mcp_previous_result_in_current_session(self):
        current = turn(
            "current", self.since + 3, "Note the previous result", status="inProgress"
        )
        self.source.turns["a"].append(current)
        self.cache.flush(self.api, scan(self.source, self.cache))

        async def check():
            with patch(
                "agent_journal.mcp_server.locate_request",
                return_value=("a", "current", self.since + 3),
            ):
                async with Client(
                    make_mcp(self.api, "test/p"), raise_exceptions=True
                ) as client:
                    response = await client.call_tool(
                        "read_journal",
                        {
                            "current_request": "Note the previous result",
                            "same_session": True,
                        },
                    )
                    self.assertEqual(
                        response.structured_content["current_turn"]["turn"], "current"
                    )
                    self.assertEqual(
                        [r["turn"] for r in response.structured_content["journey"]],
                        ["one"],
                    )

        asyncio.run(check())
