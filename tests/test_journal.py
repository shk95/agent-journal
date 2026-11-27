import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from time import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agent_journal.cli import main
from agent_journal.common import OBSERVATION_LEASE, Api, JournalHTTPError, repo_key
from agent_journal.mcp_server import make_mcp
from agent_journal.observer import Cache, scan
from agent_journal.server import SCHEMA, connect, endpoint, make_server
from agent_journal.settings import load_settings
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

    def test_server_version_preserves_the_health_payload(self):
        with urlopen(self.api.base + "/healthz", timeout=2) as response:
            self.assertEqual(
                json.load(response), {"status": "ready", "deployment": None}
            )
        with (
            patch.object(sys, "argv", ["agent-journal", "server-version"]),
            patch("builtins.print") as output,
        ):
            main()
        output.assert_called_once_with(version("agent-journal"))

    def test_storage_stats_sizes_window_and_project_scope(self):
        now = int(time())
        old = now - 31 * 86400
        with closing(connect(self.server.database)) as db, db:
            db.execute("UPDATE projects SET since=? WHERE project='test/p'", (old,))
        self.api.request("register", {"project": "test/empty"})
        records = []
        for i in range(11):
            body = "예제" * (25 if i == 0 else i)
            record = {
                "project": "test/p",
                "thread": "synthetic",
                "turn": f"fixture-{i}",
                "started": old if i == 0 else now - i,
                "status": "completed",
                "prompt": body,
                "items": [{"type": "agentMessage", "text": body}],
                "summary": "Checked the fixture",
                "revision": 1,
            }
            self.assertTrue(self.api.request("turns", record)["accepted"])
            records.append(record)
        updated = records[-1] | {
            "summary": "Updated fixture", "summary_detail": "상세 결과", "revision": 2,
        }
        self.api.request("turns", updated)
        self.api.request("turns", records[-1] | {"summary": "Stale " * 30})
        records[-1] = updated

        def body_size(record):
            text = (
                json.dumps(record["items"], ensure_ascii=False)
                + record["prompt"]
                + record["summary"]
                + record.get("summary_detail", "")
            )
            return len(text.encode())

        note = {"project": "test/p", "id": "fixture-note", "body": "메모"}
        self.api.request("notes", note)
        self.api.request("edit-note", note | {"body": "새 메모", "version": 1})
        self.api.request("disable", {"project": "test/p"})
        data = self.api.request("stats")
        self.assertGreater(data["database_bytes"], 0)
        self.assertGreaterEqual(data["wal_bytes"], 0)
        self.assertEqual(
            data["recent"],
            {
                "days": 30,
                "turn_count": 10,
                "turn_bytes": sum(map(body_size, records[1:])),
            },
        )
        empty_project, project = data["projects"]
        self.assertEqual(empty_project["project"], "test/empty")
        self.assertIsNone(empty_project["latest_turn_started"])
        self.assertEqual(empty_project["turn_bytes"] + empty_project["note_bytes"], 0)
        self.assertEqual(
            project,
            {
                "project": "test/p",
                "turn_count": 11,
                "turn_bytes": sum(map(body_size, records)),
                "note_count": 1,
                "note_bytes": len("새 메모".encode()),
                "latest_turn_started": now - 1,
            },
        )
        largest = data["largest_turns"]
        self.assertEqual(
            [row["body_bytes"] for row in largest],
            sorted(map(body_size, records), reverse=True)[:10],
        )
        self.assertEqual(largest[0]["turn"], records[0]["turn"])
        scoped = self.api.request("stats", project="test/p")
        self.assertEqual(scoped["projects"], data["projects"][1:])
        self.assertEqual(scoped["largest_turns"], largest)
        empty = self.api.request("stats", project="test/empty")
        self.assertEqual(empty["largest_turns"], [])
        self.assertEqual(empty["recent"]["turn_count"], 0)
        with self.assertRaisesRegex(JournalHTTPError, "404.*Project not found"):
            self.api.request("stats", project="test/missing")
        with self.assertRaisesRegex(JournalHTTPError, "401.*Authentication required"):
            Api(self.api.base, "wrong-token").request("stats")

    def test_storage_stats_indexes_existing_records(self):
        path = self.path / "legacy"
        with closing(connect(path)) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects VALUES ('test/legacy', 0, 1)")
            db.execute(
                """INSERT INTO turns VALUES
                   ('test/legacy', 'thread', 'turn', 1, 'completed', 'a', 'b', NULL, '[]', 1)"""
            )
            db.execute("INSERT INTO notes VALUES ('note', 'test/legacy', NULL, NULL, 'abc', 1)")
        for _ in range(2):
            with (
                make_server("127.0.0.1", 0, path, "test-token"),
                closing(connect(path)) as db,
                db,
            ):
                data = endpoint(db, "GET", "stats", {})
                self.assertEqual(data["projects"][0]["turn_bytes"], 4)
                self.assertEqual(data["projects"][0]["note_bytes"], 3)
                self.assertEqual(data["largest_turns"][0]["turn"], "turn")
                notes = endpoint(db, "GET", "notes", {"project": "test/legacy"})
                self.assertNotIn("body_bytes", notes[0])

    def test_timezone_command_sets_local_display_and_passes_it_to_web(self):
        with closing(connect(self.server.database)) as db, db:
            db.execute(
                """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,items,revision)
                   VALUES('test/p','fixture','epoch',946684800,'completed','','','[]',1)""",
            )
        with patch("builtins.print") as output:
            with patch.object(sys, "argv", ["agent-journal", "timezone"]):
                main()
            output.assert_called_once_with("Time zone · UTC (default)")
            output.reset_mock()
            with patch.object(sys, "argv", ["agent-journal", "timezone", "Asia/Seoul"]):
                main()
            output.assert_called_once_with("Time zone · Asia/Seoul")
            with patch.object(sys, "argv", ["agent-journal", "stats"]):
                main()
            self.assertTrue(any("Latest turn (Asia/Seoul)" in call.args[0] for call in output.call_args_list if call.args))
            self.assertTrue(output.call_args.args[0].endswith("2000-01-01 09:00"))
            with (
                patch.object(sys, "argv", ["agent-journal", "web", "--project", "test/p"]),
                patch("webbrowser.open_new_tab") as open_tab,
            ):
                main()
                url = urlsplit(open_tab.call_args.args[0])
                params = parse_qs(url.fragment.removeprefix("login?"))
                self.assertEqual(params["time_zone"], ["Asia/Seoul"])
                self.assertEqual(params["project"], ["test/p"])
            with (
                patch.object(sys, "argv", ["agent-journal", "timezone", "Invalid/Fixture"]),
                self.assertRaises(SystemExit),
            ):
                main()
            self.assertEqual(load_settings(self.path)["time_zone"], "Asia/Seoul")
            with patch.object(sys, "argv", ["agent-journal", "timezone", "default"]):
                main()
            output.assert_called_with("Time zone · UTC (default)")
            self.assertEqual(load_settings(self.path)["time_zone"], "default")

    def test_stats_command_reports_an_older_server(self):
        with (
            patch.object(sys, "argv", ["agent-journal", "stats"]),
            patch.object(Api, "request", side_effect=JournalHTTPError(400, "Invalid project")),
            patch("builtins.print") as output,
            self.assertRaises(SystemExit) as error,
        ):
            main()
        self.assertEqual(error.exception.code, 1)
        output.assert_called_once_with(
            "Storage statistics are unavailable on this Journal server.", file=sys.stderr
        )

    def test_session_board_filter_scopes_pages_and_turn_notes(self):
        project = "test/p"
        start = int(time())
        with closing(connect(self.server.database)) as db, db:
            db.executemany(
                """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,items,revision)
                   VALUES(?,?,?,?, 'completed', '', '', '[]', 1)""",
                [(project, "chosen", str(i), start + i) for i in range(66)]
                + [(project, "other", "earliest", start - 1), (project, "other", "latest", start + 70)],
            )
        for session, turn, identifier in (("chosen", "20", "inside"), ("other", "earliest", "outside")):
            self.api.request("notes", {"project": project, "id": identifier,
                                       "thread": session, "turn": turn, "body": "Fixture"})
        self.api.request("notes", {"project": project, "id": "project-note", "body": "Fixture"})
        self.assertEqual(self.api.request("sessions", project=project), ["chosen", "other"])
        scoped = self.api.request("board", project=project, thread="chosen")
        self.assertEqual({row["thread"] for row in scoped["turns"]}, {"chosen"})
        self.assertTrue(scoped["earlier"])
        self.assertFalse(scoped["later"])
        self.assertEqual([note["id"] for note in scoped["turn_notes"]], ["inside"])
        self.assertEqual([note["id"] for note in scoped["project_notes"]], ["project-note"])
        first = self.api.request("board", project=project, thread="chosen", edge="first")
        self.assertFalse(first["earlier"])
        self.assertTrue(first["later"])
        note_edge = self.api.request("board", project=project, thread="chosen", edge="first-note")
        self.assertEqual(note_edge["turns"][0]["turn"], "20")

    def test_session_moves_across_pages_keep_chronological_context(self):
        project = "test/p"
        with closing(connect(self.server.database)) as db, db:
            db.executemany(
                """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,items,revision)
                   VALUES(?,?,?,?, 'completed', '', '', '[]', 1)""",
                [(project, "interleaved", str(i), i) for i in range(1, 130)]
                + [(project, "selected", "early", 3), (project, "selected", "late", 125)],
            )
        early, late = [3, "early", "selected"], [125, "late", "selected"]
        for boundary, origin, target, index in (("before", late, early, -1), ("after", early, late, 0)):
            with self.subTest(boundary=boundary):
                moved = self.api.request("board", project=project, **{"session_" + boundary: json.dumps(origin)})
                self.assertEqual(moved["session_target"], target)
                self.assertEqual(moved["turns"][index]["turn"], target[1])
                self.assertIn("interleaved", {row["thread"] for row in moved["turns"]})
                pinned = self.api.request(
                    "board", project=project, inclusive="true", **{boundary: json.dumps(target)},
                )
                self.assertEqual(moved["turns"], pinned["turns"])
        no_move = self.api.request("board", project=project, session_before=json.dumps(early))
        self.assertIsNone(no_move["session_target"])

    def test_note_titles_move_delete_and_scoped_board(self):
        project = "test/p"
        now = int(time())
        with closing(connect(self.server.database)) as db, db:
            db.executemany(
                """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,items,revision)
                   VALUES(?,?,?,?, 'completed', '', '', '[]', 1)""",
                [(project, "example-thread", str(i), now - 80 + i) for i in range(66)],
            )
        body = "## 예제 제목\n\n  한글\n\n    mixed 👩‍💻\n"
        new_note = {
            "project": project, "id": "new-note", "thread": "example-thread",
            "turn": "65", "format": "markdown", "body": body, "priority": "highlighted",
        }
        self.assertEqual(
            self.api.request("notes", new_note),
            {"id": "new-note", "version": 1, "matches": True},
        )
        old_note = new_note | {"id": "older-note", "turn": "1", "body": "old " * 1000}
        self.api.request("notes", old_note)
        page = self.api.request("board", project=project)
        self.assertEqual([row["id"] for row in page["turn_notes"]], ["new-note"])
        self.assertNotIn("body", page["turn_notes"][0])
        same = self.api.request("board", project=project, revision=page["revision"])
        self.assertTrue(same["unchanged"])
        oldest = self.api.request("board", project=project, edge="first-note")
        self.assertEqual(oldest["turns"][0]["turn"], "1")
        self.assertTrue(oldest["later"])
        detail = self.api.request("note", project=project, id="new-note")
        self.assertEqual((detail["title"], detail["body"]), ("예제 제목", body))
        move = new_note | {"turn": "10", "version": 1}
        moved = self.api.request("move-note", move)
        self.assertEqual(moved["priority"], "highlighted")
        self.assertEqual(moved["version"], 2)
        self.assertEqual(self.api.request("move-note", move), moved)
        self.assertEqual((moved["created"], moved["title"], moved["body"]),
                         (detail["created"], detail["title"], body))
        with self.assertRaisesRegex(JournalHTTPError, "409"):
            self.api.request("delete-note", move)
        with self.assertRaisesRegex(JournalHTTPError, "404"):
            self.api.request("move-note", move | {"version": 2, "turn": "missing"})
        self.assertEqual(self.api.request("note", project=project, id="new-note")["turn"], "10")
        self.api.request("delete-note", move | {"version": 2})
        self.api.request("delete-note", move | {"version": 2})
        self.assertEqual(
            self.api.request("stats", project=project)["projects"][0]["note_bytes"],
            len(old_note["body"].encode("utf-8")),
        )

    def test_note_status_and_body_share_version_and_keep_their_values(self):
        key = {"id": "priority-note", "project": "test/p"}
        source = "# Example\n\nOriginal"
        self.api.request("notes", key | {"body": source})
        self.assertEqual(self.api.request("note", **key)["priority"], "normal")
        update = key | {"version": 1, "priority": "low"}
        row = self.api.request("set-note-priority", update)
        self.assertEqual((row["version"], row["priority"], row["body"]), (2, "low", source))
        self.assertEqual(self.api.request("set-note-priority", update), row)
        with self.assertRaisesRegex(JournalHTTPError, "400"):
            self.api.request("set-note-priority", update | {"priority": "unknown"})
        with self.assertRaisesRegex(JournalHTTPError, "409"):
            self.api.request("set-note-priority", update | {"priority": "highlighted"})
        self.api.request("edit-note", key | {"version": 2, "body": "Replacement"})
        summary = self.api.request("board", project="test/p")["project_notes"][0]
        self.assertEqual((summary["created"], summary["priority"]), (row["created"], "low"))

    def test_markdown_title_updates_and_ignores_headings_inside_other_blocks(self):
        fields = {"project": "test/p", "id": "markdown-note", "format": "markdown"}
        source = "```text\n# Code\n```\n\n> # Quote\n\n    # Indented\n\n한글 **샘플**\n===\n"
        self.api.request("notes", fields | {"body": source})
        note = self.api.request("note", project="test/p", id="markdown-note")
        self.assertEqual((note["title"], note["body"]), ("한글 샘플", source))
        untitled = source.split("한글")[0] + "Plain paragraph."
        self.api.request("edit-note", fields | {"body": untitled, "version": 1})
        note = self.api.request("board", project="test/p")["project_notes"][0]
        self.assertEqual(note["title"], "Untitled note")
        self.api.request("edit-note", fields | {"body": "# New heading\n", "version": 2})
        self.assertEqual(
            self.api.request("note", project="test/p", id="markdown-note")["title"],
            "New heading",
        )

    def test_migration_promotes_existing_title_to_markdown_once(self):
        path = self.path / "legacy"
        with closing(connect(path)) as db:
            db.executescript(SCHEMA)
            with db:
                db.execute("ALTER TABLE notes ADD COLUMN title TEXT NOT NULL DEFAULT ''")
                db.execute("INSERT INTO projects VALUES('test/p',0,1)")
                db.execute(
                    "INSERT INTO notes(id, project, title, body, created) VALUES(?, ?, ?, ?, 1)",
                    ("legacy", "test/p", "한글 *fixture* #", "## Keeping the original body"),
                )
        for _ in range(2):
            make_server("127.0.0.1", 0, path, "test-token").server_close()
            with closing(connect(path)) as db:
                row = db.execute("SELECT title,body,version FROM notes").fetchone()
                self.assertEqual(
                    tuple(row), ("한글 *fixture* #", r"# 한글 \*fixture\* \#" + "\n\n## Keeping the original body", 1)
                )

    def test_web_command_opens_configured_journal(self):
        for options, project, fragment in (
            ([], "", ""),
            (["--project", "Test/P"], "test/p", "#test%2Fp"),
        ):
            with (
                self.subTest(options=options),
                patch.object(sys, "argv", ["agent-journal", "web", *options]),
                patch("webbrowser.open_new_tab") as open_tab,
                patch("builtins.print") as print_url,
            ):
                main()
                url = self.api.base + "/" + fragment
                print_url.assert_called_once_with(url, flush=True)
                open_tab.assert_called_once()
                opened = open_tab.call_args.args[0]
                root, login = opened.split("#login?", 1)
                self.assertEqual(root, self.api.base + "/")
                fields = parse_qs(login, keep_blank_values=True)
                self.assertEqual(fields["project"], [project])
                self.assertNotIn(self.api.token, opened)
                request = Request(
                    root + "api/login", json.dumps({"code": fields["code"][0]}).encode(),
                    headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"},
                )
                with urlopen(request) as response:
                    session, *attributes = response.headers["Set-Cookie"].split("; ")
                self.assertIn("HttpOnly", attributes)
                self.assertIn("SameSite=Strict", attributes)
                self.assertIn("Secure", attributes)
                self.assertIn("Max-Age=31536000", attributes)
                with urlopen(Request(root + "api/projects", headers={"Cookie": session})) as response:
                    self.assertEqual(json.load(response)[0]["project"], "test/p")
                with self.assertRaises(HTTPError) as replayed:
                    urlopen(request)
                self.assertEqual(replayed.exception.code, 401)
                with self.assertRaises(HTTPError) as cookie_only:
                    urlopen(Request(root + "api/browser-login", b"{}", headers={
                        "Content-Type": "application/json", "Cookie": session,
                    }))
                self.assertEqual(cookie_only.exception.code, 401)

    def test_web_command_opens_plain_url_with_an_older_server(self):
        with (
            patch.object(sys, "argv", ["agent-journal", "web"]),
            patch.object(Api, "request", side_effect=JournalHTTPError(400, "Invalid project")),
            patch("webbrowser.open_new_tab") as open_tab,
            patch("builtins.print") as print_url,
        ):
            main()
        url = self.api.base + "/"
        open_tab.assert_called_once_with(url)
        print_url.assert_called_once_with(url, flush=True)

    def test_browser_login_code_expires(self):
        with patch("agent_journal.server.BROWSER_LOGIN_TTL", 0):
            code = self.api.request("browser-login", {})["code"]
        with self.assertRaisesRegex(JournalHTTPError, "401"):
            self.api.request("login", {"code": code})

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
            open_tab.assert_called_once()
            fragment = urlsplit(open_tab.call_args.args[0]).fragment
            self.assertTrue(fragment.startswith("login?code="))
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
        while work_once(self.cache.db, lambda record, previous: {
            "summary": "semantic result", "summary_detail": "The fixture confirmed the result.",
        }):
            pass
        self.cache.flush(self.api)
        self.api.request("turns", payload)
        self.assertEqual(
            [r["summary"] for r in self.rows()], ["semantic result", "semantic result"]
        )
        detail = self.api.request("transcript", project="test/p", thread="a", turn="one", with_summary="true")
        self.assertEqual(detail["summary_detail"], "The fixture confirmed the result.")
        self.assertEqual(detail["items"], self.api.request("transcript", project="test/p", thread="a", turn="one"))

    def test_summary_context_crosses_pages_within_the_collection_period(self):
        start = int(self.since) + 1
        self.source.projects = {"a": "p"}
        self.source.turns = {"a": [
            turn("uncollected", start - 2, "Uncollected request"),
            *(turn(f"sample-{i}", start + i, f"Request {i}", f"Result {i}") for i in range(8)),
        ]}
        original_pages = self.source.pages

        def small_pages(method, params):
            for rows in original_pages(method, params):
                for index in range(0, len(rows), 2):
                    yield rows[index : index + 2]

        with patch.object(self.source, "pages", small_pages):
            scan(self.source, self.cache)
        contexts = {
            key: json.loads(context)["previous"]
            for key, context in self.cache.db.execute("SELECT turn,context FROM summaries")
        }
        self.assertEqual(contexts["sample-0"], [])
        self.assertEqual(contexts["sample-7"], [
            {"request": f"Request {index}", "answer": f"Result {index}"}
            for index in range(1, 7)
        ])

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

        def finish_old_job(record, previous):
            self.source.turns["a"][-1]["items"][-1]["text"] = "revised final"
            scan(self.source, self.cache)
            return {"summary": "obsolete summary", "summary_detail": "Obsolete detail."}

        self.assertTrue(work_once(self.cache.db, finish_old_job))
        self.cache.flush(self.api)
        self.assertEqual(self.rows()[0]["summary"], "revised final")
        while work_once(self.cache.db, lambda record, previous: {
            "summary": "latest summary", "summary_detail": "Latest detail.",
        }):
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

    def test_note_groups_and_projects_keep_separate_notes(self):
        group, project, standalone = ("~/" + key * 32 for key in "abc")
        group_payload = {"project": group, "kind": "group", "name": " Fixture group "}
        child_payload = {"project": project, "kind": "project", "name": "Fixture child", "parent": group}
        for payload in (group_payload, child_payload, {"project": standalone, "kind": "project", "name": "Fixture project"}):
            self.assertEqual(self.api.request("note-spaces", payload), {"project": payload["project"]})
        self.assertEqual(self.api.request("note-spaces", group_payload), {"project": group})
        with self.assertRaisesRegex(RuntimeError, "409"):
            self.api.request("note-spaces", group_payload | {"name": "Another group"})
        with self.assertRaisesRegex(RuntimeError, "400"):
            self.api.request("note-spaces", child_payload | {"parent": standalone})
        self.assertEqual([row["project"] for row in self.api.request("projects")], ["test/p"])
        overview = {row["project"]: row for row in self.api.request("overview")}
        self.assertEqual(overview.keys(), {"test/p", group, standalone})
        for scope, name, kind in ((group, "Fixture group", "group"), (standalone, "Fixture project", "project")):
            self.assertEqual(overview[scope], {
                "project": scope, "name": name, "kind": kind, "active": 0, "turns": [],
            })
        for scope, title in ((group, "Group memo"), (project, "Project memo")):
            note = {"project": scope, "id": title, "body": f"# {title}"}
            self.api.request("notes", note)
            with self.assertRaisesRegex(RuntimeError, "400"):
                self.api.request("notes", note | {"thread": "fixture", "turn": "fixture"})
            with self.assertRaisesRegex(RuntimeError, "400"):
                self.api.request("register", {"project": scope})
            board = self.api.request("board", project=scope)
            self.assertEqual(board["turns"], [])
            self.assertEqual([row["title"] for row in board["project_notes"]], [title])
        children = self.api.request("board", project=group)["space"]["children"]
        self.assertEqual(children, [{"project": project, "name": "Fixture child"}])
        self.assertEqual(self.api.request("board", project=project)["space"]["parent_name"], "Fixture group")
        self.assertEqual(self.api.request("stats", project=group)["projects"][0]["note_count"], 1)

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

    def test_mcp_can_read_and_set_the_local_explanation_quality(self):
        async def check():
            async with Client(make_mcp(), raise_exceptions=True) as client:
                initial = await client.call_tool("explanation_settings", {})
                self.assertEqual(initial.structured_content, {
                    "explanation_quality": "Contextual", "measure_explanation_usage": True,
                })
                self.assertFalse((self.path / "settings.json").exists())
                updated = await client.call_tool("set_explanation_quality", {"quality": "심층"})
                self.assertEqual(updated.structured_content, {"explanation_quality": "In-depth"})
                self.assertEqual(load_settings(self.path)["explanation_quality"], "In-depth")
                usage = await client.call_tool("explanation_usage", {"days": 7})
                self.assertEqual(usage.structured_content, {"days": 7, "usage": []})

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
