import curses
import time
import unittest
from concurrent.futures import Future
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_journal.client_state import ClientState
from agent_journal.common import JournalHTTPError
from agent_journal.text_layout import fit, markdown_preview, wrap
from agent_journal.tui import Terminal
from agent_journal.vi import Editor, Paste, editor_rows


class FixtureApi:
    base = "http://127.0.0.1"
    token = "synthetic-token"

    def __init__(self):
        self.writes = []
        self.offline = False
        self.conflict = False

    def request(self, path, payload):
        self.writes.append((path, payload))
        if self.offline:
            raise OSError("Offline fixture")
        if self.conflict:
            raise JournalHTTPError(409, "Conflict fixture")
        return {"version": int(payload.get("version", 0)) + 1}


class EditingTests(unittest.TestCase):
    def test_terminal_opens_web_with_the_current_connection(self):
        api = FixtureApi()
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"AGENT_JOURNAL_HOME": directory}),
            patch("agent_journal.tui.terminal_palette", return_value=([], 0)),
            patch.object(api, "request", return_value={"code": "fixture-code"}) as request,
            patch("webbrowser.open_new_tab", return_value=True) as open_tab,
        ):
            terminal = Terminal(None, api, "example/fixture")
            terminal.refresh_after["{}"] = float("inf")
            self.assertTrue(terminal.list_key("w"))
            terminal.web_job.result(timeout=2)
            terminal.poll()
            request.assert_called_once_with("browser-login", {})
            open_tab.assert_called_once_with(api.base + "/#login?code=fixture-code&project=example%2Ffixture")
            self.assertEqual(terminal.message, "Web journal opened")

    def test_terminal_keeps_only_latest_waiting_detail_request(self):
        first, latest = Future(), Future()
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"AGENT_JOURNAL_HOME": directory}),
            patch("agent_journal.tui.terminal_palette", return_value=([], 0)),
            patch("agent_journal.tui.background", side_effect=(first, latest)) as background,
        ):
            terminal = Terminal(None, FixtureApi(), "example/fixture")
            terminal.refresh_after["{}"] = float("inf")
            for turn in ("first", "discarded", "latest"):
                if terminal.item:
                    terminal.close_item()
                terminal.open_transcript({"thread": "session", "turn": turn, "prompt": turn})
            self.assertEqual(background.call_count, 1)
            first.set_result(("Old summary", "Old result"))
            terminal.poll()
            self.assertEqual(background.call_count, 2)
            self.assertEqual(background.call_args.args[-1]["turn"], "latest")
            self.assertTrue(terminal.item.loading)
            latest.set_result(("New summary", "New result"))
            terminal.poll()
            self.assertEqual(terminal.item.content, "New result")
            self.assertEqual(terminal.item.summary.content, "New summary")

    def test_terminal_reopens_acknowledged_text_from_a_stale_board(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"AGENT_JOURNAL_HOME": directory}),
            patch("agent_journal.tui.terminal_palette", return_value=([], 0)),
        ):
            note = {"id": "note", "turn": None, "thread": None, "version": 1, "body": "Old text"}
            terminal = Terminal(None, FixtureApi(), "example/fixture")
            terminal.details[("note", 1)] = note
            terminal.state.recoveries["note"] = note | {"version": 2, "body": "Saved text"}
            terminal.open_note(note, [], edit=True)
            self.assertEqual(terminal.item.editor.note["body"], "Saved text")
            terminal.item.editor.note["body"] += " plus another edit"
            terminal.save(immediate=True)
            draft = terminal.state.drafts["note"]
            self.assertEqual((draft["version"], draft["body"]), (2, "Saved text plus another edit"))

    def test_terminal_force_quit_discards_the_draft_while_a_save_is_running(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"AGENT_JOURNAL_HOME": directory}),
            patch("agent_journal.tui.terminal_palette", return_value=([], 0)),
        ):
            terminal = Terminal(None, FixtureApi(), "example/fixture")
            note = {"id": "note", "turn": None, "thread": None, "version": 1, "body": "Server text"}
            terminal.open_note(note, [], edit=True)
            for key in "A!":
                terminal.detail_key(key)
            terminal.save(immediate=True)
            job = Future()
            terminal.state.saves["note"] = job, terminal.state.drafts["note"].copy()
            terminal.detail_key("?")
            terminal.save(immediate=True)
            for key in ("\x1b", ":", "q", "!", "\n"):
                terminal.detail_key(key)
            self.assertIsNone(terminal.item)
            self.assertNotIn("note", terminal.state.drafts)
            self.assertFalse(terminal.state.draft_path("note").exists())
            job.set_result(2)
            terminal.state.poll(time.time())
            self.assertNotIn("note", terminal.state.drafts)
            self.assertNotIn("note", terminal.state.saves)
            terminal.open_note(note, [], edit=True)
            self.assertEqual(terminal.item.editor.text, "Server text!")

    def test_graphemes_are_intact_in_display_and_editing(self):
        self.assertEqual(fit("한글", 2), "한")
        self.assertEqual(fit("e\u0301", 1), "é")
        self.assertEqual(list(wrap("A👩‍💻가", 2)), ["A", "👩‍💻", "가"])
        self.assertEqual(fit("\x1b[31m한\x1b[0m", 2), "한")
        editor = Editor({"body": ""}, mode="Insert")
        self.assertEqual(editor.input(Paste("한글\r\n    👩‍💻")), "change")
        self.assertEqual(editor.note["body"], "한글\n    👩‍💻")
        self.assertEqual(editor.input("\x7f"), "change")
        self.assertEqual(editor.note["body"], "한글\n    ")
        self.assertIn("│   sample", markdown_preview("```\n  sample\n```"))

    def test_markdown_preview_keeps_line_and_paragraph_breaks(self):
        source = "First line\nSecond line\\\nThird line  \nFourth line\n\nNext paragraph"
        self.assertEqual(
            list(wrap(markdown_preview(source), 80)),
            ["First line", "Second line", "Third line", "Fourth line", "", "Next paragraph"],
        )

    def test_editor_wraps_for_display_and_only_closes_on_command(self):
        body = "one two three\n한글👩‍💻\n"
        editor = Editor({"body": body}, mode="Insert")
        self.assertEqual(editor_rows(body, 8), [
            (0, "one two ", False), (8, "three", True), (14, "한글👩‍💻", False), (20, "", False),
        ])
        self.assertEqual(list(wrap("hello world", 5)), ["hello", " worl", "d"])
        self.assertEqual(list(wrap("한글 메모", 4)), ["한글", " 메", "모"])
        self.assertEqual(list(wrap("one two three\nend", 8, mark=True)), ["  one tw", "↳ o thre", "↳ e", "  end"])
        editor.width = 8
        editor.position = 2
        editor.input(curses.KEY_DOWN)
        self.assertEqual(editor.position, 10)
        editor.input(curses.KEY_UP)
        self.assertEqual(editor.position, 2)
        self.assertIsNone(editor.input("\x1b"))
        self.assertEqual(editor.mode, "Normal")
        for key in ("\x1b", ":", "w", "\x1b", "\n", ":", "q", "!"):
            self.assertIsNone(editor.input(key))
        self.assertEqual(editor.input("\n"), "discard-close")
        for key in (":", "q"):
            self.assertIsNone(editor.input(key))
        self.assertEqual(editor.input("\n"), "close")
        self.assertEqual(editor.note["body"], body)

    def test_vi_word_motions_and_line_insert_commands(self):
        text = "  alpha.beta next\n  finish"
        editor = Editor({"body": text})
        for keys, expected in (("^", 2), ("e", 6), ("w", 7), ("w", 8), ("e", 11), ("W", 13), ("B", 2)):
            editor.input(keys)
            self.assertEqual(editor.position, expected)
        editor.input("A")
        self.assertEqual((editor.mode, editor.position), ("Insert", text.index("\n")))
        editor.input("!")
        editor.input("\x1b")
        editor.input("I")
        self.assertEqual((editor.mode, editor.position), ("Insert", 2))
        editor.input("+")
        self.assertEqual(editor.note["body"], "  +alpha.beta next!\n  finish")

    def test_detail_preview_omits_only_a_leading_title(self):
        note = "한글 **노트**\n===\n\nText.\n\n## Keep this heading"
        self.assertEqual(markdown_preview(note, True), "Text.\n\nKeep this heading")
        self.assertEqual(markdown_preview("Intro.\n\n" + note, True), markdown_preview("Intro.\n\n" + note))
        self.assertIn("한글 *노트*", markdown_preview(note))

    def test_draft_keeps_latest_text_after_ack_and_survives_conflict(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"AGENT_JOURNAL_HOME": directory}),
        ):
            api = FixtureApi()
            state = ClientState(api, "example/fixture")
            note = {
                "id": "sample",
                "body": "첫 줄",
                "thread": None,
                "turn": None,
                "version": None,
            }
            state.edit(note, immediate=True)
            state.poll(time.time())
            state.edit(note | {"body": "새 줄"})
            state.saves[note["id"]][0].result(timeout=2)
            state.poll(time.time())
            self.assertEqual(state.drafts[note["id"]]["version"], 1)
            fresh = ClientState(api, "example/fixture")
            self.assertEqual(fresh.drafts[note["id"]]["body"], "새 줄")
            api.conflict = True
            fresh.poll(time.time() + 2)
            self.assertRaises(JournalHTTPError, fresh.saves[note["id"]][0].result, 2)
            fresh.poll(time.time())
            self.assertTrue(fresh.drafts[note["id"]]["conflict"])
            self.assertEqual(
                ClientState(api, "example/fixture").drafts[note["id"]]["body"], "새 줄"
            )
