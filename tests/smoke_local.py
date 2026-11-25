"""Exercise a real Codex App Server and the journal clients on loopback."""

import argparse
import ast
import contextlib
import json
import os
import re
import secrets
import select
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_journal.codex_integration import collector_lock
from agent_journal.common import Api
from agent_journal.observer import Codex
from scripts.dev import FIXTURE, seed
from tests.processes import (
    child, cleanup_on_termination, observer_groups, startup_line, stop_group, stop_tmux,
)
from tests.workspace import test_workspace

PROJECT = "example/journal-smoke"
REGISTER_TASK = "Register this repository with Agent Journal."
TASK = "Verify the sample import."
NOTE_TASK = "Attach a note to the previous result."
ANSWER = "The sample import passed."
SUMMARY = "Verified the sample import."
NOTE_SUMMARY = "Attached a regression note to the sample import."
NOTE = "Keep a regression case."
EDIT = "Keep the regression fixture with the import."
PROJECT_NOTE = "Review other import formats later."
MOUSE_NOTE = "Keep the input in the example."
MOUSE_EDIT = "Save the input with its example."


def legacy_browser_pages(browser, api, cookies):
    project = "example/legacy-browser"
    api.request("register", {"project": project})
    since = next(row["since"] for row in api.request("projects") if row["project"] == project)
    for index in range(140):
        api.request("turns", {
            "project": project, "thread": "legacy", "turn": f"sample-{index:03}",
            "started": int(since) + 1 + index, "prompt": f"Legacy sample {index:03}",
            "summary": "Recorded sample", "items": [], "status": "completed", "revision": 1,
        })
    api.request("notes", {"project": project, "id": "legacy-note", "body": "# Legacy note\n\nSample body"})
    with browser.new_context(service_workers="block") as context:
        context.add_cookies(cookies)
        page = context.new_page()
        page.route("**/api/board?*", lambda route: route.fulfill(status=404, json={}))
        page.route("**/api/note?*", lambda route: route.fulfill(status=404, json={}))
        page.goto(api.base + "/#" + project)
        rows = page.locator(".rowbtn")
        for command, first in (
            (None, 80), ("Earlier turns", 20), ("Earlier turns", 0),
            ("Newer turns", 20), ("Latest turns", 80), ("Oldest turns", 0),
            ("Newer turns", 20), ("Newer turns", 80),
        ):
            if command:
                page.get_by_role("button", name=command, exact=True).click()
            rows.first.get_by_text(f"Legacy sample {first:03}", exact=True).wait_for()
        page.get_by_role("button", name="Legacy note", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_text("Sample body", exact=True).wait_for()
        dialog.get_by_role("button", name="Edit", exact=True).click()
        dialog.get_by_label("Note text").fill("# Edited legacy note\n\nSample body")
        dialog.get_by_role("button", name="Save", exact=True).click()
        wait_for("the legacy note edit to reach the server", lambda:
                 api.request("notes", project=project)[0]["body"].startswith("# Edited legacy note"))


class FixtureModel(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"models":[]}')

    def event(self, event, **values):
        payload = json.dumps({"type": event, **values})
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def response(self, item):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {
            "id": "fixture-" + item["id"],
            "status": "in_progress",
            "output": [],
        }
        self.event("response.created", response=response)
        self.event("response.output_item.added", output_index=0, item=item)
        self.event("response.output_item.done", output_index=0, item=item)
        self.event(
            "response.completed",
            response=response | {"status": "completed", "output": [item]},
        )

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = data.get("input", [])
        serialized = json.dumps(inputs)
        outputs = {
            item.get("call_id"): item.get("output")
            for item in inputs
            if item.get("type") == "function_call_output"
        }
        name = call_id = None
        if "observations for the journal" in serialized:
            if NOTE_TASK in serialized:
                self.server.note_summary_started.set()
                self.server.release_note_summary.wait(timeout=15)
                answer = NOTE_SUMMARY
            else:
                answer = SUMMARY
        elif REGISTER_TASK in serialized:
            if "fixture-register" not in outputs:
                name, call_id = "register_project", "fixture-register"
                arguments = {}
            answer = "Registered the fixture project."
        elif NOTE_TASK in serialized:
            if "fixture-read" not in outputs:
                name, call_id = "read_journal", "fixture-read"
                arguments = {
                    "project": PROJECT,
                    "current_request": NOTE_TASK,
                    "same_session": True,
                }
            elif "fixture-write" not in outputs:
                returned = json.dumps(outputs["fixture-read"])
                target = self.server.target
                if target["turn"] not in returned or "journey" not in returned:
                    self.send_error(
                        400, "The journal did not return the expected previous turn"
                    )
                    return
                name, call_id = "add_note", "fixture-write"
                arguments = {
                    "project": PROJECT,
                    "request_id": call_id,
                    "body": "# " + NOTE,
                    **target,
                }
            answer = "Saved the requested turn note."
        else:
            # Leave enough time for the observer to publish a running snapshot.
            time.sleep(3)
            answer = ANSWER

        if name:
            item = {
                "id": call_id,
                "type": "function_call",
                "namespace": "mcp__journal",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        else:
            item = {
                "id": "fixture-message",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": answer, "annotations": []}],
            }
        self.response(item)


def wait_for(label, check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {label}")


def provider_config(url):
    return [
        "-c",
        'model_provider="journal_fixture"',
        "-c",
        'model="gpt-5.4"',
        "-c",
        'model_providers.journal_fixture.name="Journal fixture"',
        "-c",
        "model_providers.journal_fixture.base_url=" + json.dumps(url),
        "-c",
        'model_providers.journal_fixture.wire_api="responses"',
        "-c",
        "model_providers.journal_fixture.supports_websockets=false",
    ]


def start_turn(codex, session, request):
    return codex.call(
        "turn/start",
        {"threadId": session, "input": [{"type": "text", "text": request}]},
    )["turn"]["id"]


def check_clients(path, repo, cli, env, api, channel):
    from playwright.sync_api import sync_playwright

    tmux = ["tmux", "-S", str(path / "tm")]
    nested = repo / "nested"
    nested.mkdir()
    profile = "xterm-ghostty" if os.environ.get("TERM") == "xterm-ghostty" else "xterm-256color"
    def launch():
        subprocess.run(
            tmux
            + [
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "journal",
                "-x",
                "125",
                "-y",
                "30",
                "-c",
                str(nested),
                f"TERM={profile} {shlex.quote(str(cli))}",
            ],
            check=True,
            timeout=10,
            env=env | {"TERM": "xterm-256color"},
        )

    def screen():
        return subprocess.check_output(
            tmux + ["capture-pane", "-p", "-t", "journal"], text=True
        )

    def mouse(button, x, y, click=False):
        sgr = (
            subprocess.check_output(
                tmux + ["display-message", "-p", "-t", "journal", "#{mouse_sgr_flag}"],
                text=True,
            ).strip()
            == "1"
        )
        if sgr:
            position = f"\x1b[<{button};{x + 1};{y + 1}".encode()
            event = position + b"M"
            if click:
                event += position + b"m"
        else:
            position = bytes((x + 33, y + 33))
            event = b"\x1b[M" + bytes((button + 32,)) + position
            if click:
                event += b"\x1b[M#" + position
        subprocess.run(
            tmux
            + ["send-keys", "-H", "-t", "journal", *[f"{byte:02x}" for byte in event]],
            check=True,
        )

    def click_label(label, offset=0):
        y, line = next(
            (i, row) for i, row in reversed(list(enumerate(screen().splitlines())))
            if label in row
        )
        mouse(0, line.index(label) + offset, y, click=True)

    def selected(prompt):
        return any(
            ">" in row[:8] and prompt in row for row in screen().splitlines()
        )

    def axis(label):
        return "[Back] Esc Axes · " + label in screen()

    def action_bar(*labels):
        lines = screen().splitlines()
        return any(
            all(label in row for label in labels) and re.fullmatch(r"│─+│", lines[i + 1])
            for i, row in enumerate(lines[:-1])
        )

    def send(*keys):
        subprocess.run(tmux + ["send-keys", "-t", "journal", *keys], check=True)

    def paste(value):
        subprocess.run(tmux + ["set-buffer", "--", value], check=True)
        subprocess.run(tmux + ["paste-buffer", "-p", "-t", "journal"], check=True)

    try:
        launch()
        wait_for("the TUI journey for the current checkout", lambda: TASK in screen())
        assert "[Transcript] t" in screen() and "[+ Turn note] m" in screen()
        assert "[Older]" not in screen() and "[Newer]" not in screen()
        send("[")
        wait_for("feedback at the oldest page", lambda: "No older turns" in screen())
        if screen().splitlines()[0] != PROJECT:
            raise RuntimeError("The default TUI did not select the checkout's project")
        turn_row = next(i for i, row in enumerate(screen().splitlines()) if TASK in row)
        lines = screen().splitlines()
        label = "● " + api.request("journey", project=PROJECT)[0]["thread"][-6:]
        assert label in lines[turn_row]
        assert any(NOTE_TASK in row and label in row for row in lines)
        if [line[2] for line in lines[turn_row : turn_row + 4]] != ["●", "│", "│", "●"]:
            raise RuntimeError("The terminal session line stops at the card boundary")
        if not re.match(r"^┌─+┐ ┌", lines[turn_row - 1]):
            raise RuntimeError("The terminal session graph needs its own frame")
        graph, separator, cards = lines[turn_row + 2].partition(" ├")
        if not separator or set(graph) - {"│", " "}:
            raise RuntimeError("Card borders must stop outside the terminal session graph")
        boundary = re.match(r"^─+┼─+┤", cards)
        header = lines[turn_row - 1]
        if not boundary or len(graph + separator + boundary[0]) - 1 != header.index("┐", len(graph)):
            raise RuntimeError("The card separator must extend across both turn columns")
        click_label("[1 Journey]")
        wait_for("the Journey axis view", lambda: axis("1 Journey"))
        click_label("[2 Turn notes]")
        wait_for("the combined axis view", lambda: axis("1 Journey + 2 Turn notes"))
        wait_for(
            "the turn and its note inside one pair of columns",
            lambda: any(TASK in row and NOTE in row and "│" in row for row in screen().splitlines()),
        )
        send("2")
        wait_for("the single axis again", lambda: "Axes · 1 Journey +" not in screen())
        send("2")
        wait_for("the restored combined axis view", lambda: axis("1 Journey + 2 Turn notes"))
        send("g", "g")
        wait_for("the first turn selected", lambda: selected(TASK))
        send("l")
        wait_for("the note on the same turn selected with l", lambda:
                 re.search(r"› (?:\[\d+/\d+\] )?" + re.escape(NOTE), screen()))
        send("l", "Enter")
        wait_for("the rightmost axis to stay active", lambda: "Turn note · " + TASK in screen())
        send("Escape", "h")
        wait_for("the original turn focused with h", lambda:
                 "[Close]" not in screen() and selected(TASK) and "› " + NOTE not in screen())
        click_label("[Transcript]")
        wait_for("the clicked turn's transcript and its parent", lambda:
                 ANSWER in screen() and action_bar("[Close] Esc") and "Axes ·" in screen())
        layers = screen().rstrip("\n").splitlines()
        for row in (3, 6):
            if not re.fullmatch("┌─+┐", layers[row]):
                raise RuntimeError("Modal layers must share the entire pane width")
        if not (
            axis("1 Journey + 2 Turn notes") and "Axes ·" in layers[5]
            and all(line.startswith("│") and line.endswith("│") for line in layers[7:-1])
            and re.fullmatch("└─+┘", layers[-1])
        ):
            raise RuntimeError("Only the top edges of parent layers should remain visible")
        send("d", "u", "G", "g", "g")
        click_label("[Close]")
        wait_for("the restored axes", lambda:
                 axis("1 Journey + 2 Turn notes") and selected(TASK) and "[Close]" not in screen())
        click_label("[+ Turn note]")
        wait_for("the in-place turn note editor", lambda:
                 "Insert ·" in screen() and action_bar("[Close] Esc", "[Save] :w", "[Preview] ^P"))
        title = "한글 노트"
        paragraph = "Next paragraph."
        markdown = f"# {title}\n\n{MOUSE_NOTE}\n    Indented line\\\nHard break.\n\n{paragraph}"
        expected_lines = [MOUSE_NOTE, "Indented line", "Hard break.", "", paragraph]

        def shows_note_lines(*, preview=False):
            rows = screen().splitlines()
            x = 1
            if preview:
                x = next((row.index("Markdown preview") for row in rows if "Markdown preview" in row), -1)
                if x == -1:
                    return False
            content = [row[x:-1].strip() for row in rows]
            return any(content[i : i + len(expected_lines)] == expected_lines for i in range(len(rows)))

        paste(markdown)
        send("C-p")
        wait_for("line and paragraph breaks in the TUI note preview", lambda: shows_note_lines(preview=True))
        send("Escape", ":", "w", "q", "Enter")
        first_turn = api.request("journey", project=PROJECT)[0]["turn"]
        wait_for("the saved title and indented contents", lambda: any(
            note["turn"] == first_turn and note["title"] == title
            and note["body"] == markdown
            for note in api.request("notes", project=PROJECT)
        ))
        wait_for("the editor to close", lambda: "[Close]" not in screen())
        wait_for("the Korean note in the board", lambda: title in screen())
        click_label(title)
        wait_for("line and paragraph breaks in the TUI note detail", shows_note_lines)
        assert screen().count(title) == 1
        assert action_bar("[Close] Esc", "[Edit] e", "[Status] s", "[Cut] x", "[Delete] D")
        click_label("[Status] s", offset=len("[Status] "))
        wait_for("the normal note status", lambda: "Status · Normal" in screen())
        assert action_bar("[Highlighted] 1", "[Normal] 2", "[Low priority] 3", "[Cancel] Esc")
        send("1")
        wait_for("the highlighted note marker", lambda: "★ " + title in screen())
        click_label("[Delete]")
        wait_for("the delete confirmation actions", lambda: action_bar("[Cancel]", "[Delete]"))
        click_label("[Cancel]")
        wait_for("the note actions after cancelling", lambda: action_bar("[Close]", "[Edit]", "[Cut]", "[Delete]"))
        send("Escape")
        click_label("[Back]")
        wait_for("the overview and its original selection", lambda: selected(NOTE_TASK))
        send("n")
        wait_for("the in-place project note editor", lambda: "Insert ·" in screen())
        paste(PROJECT_NOTE)
        wait_for("the autosaved project note", lambda: any(
            note["title"] == "Untitled note" and note["body"] == PROJECT_NOTE and note["turn"] is None
            for note in api.request("notes", project=PROJECT)
        ))
        send("Escape", "Escape")
        wait_for("the project axis after editing", lambda: axis("3 Project notes") and "[Close]" not in screen())
        send("Escape", "2")
        wait_for("the turn notes axis", lambda: axis("2 Turn notes"))
        send("g", "g")
        wait_for(
            "the first turn note selected",
            lambda: re.search(r"› (?:\[\d+/\d+\] )?" + re.escape(NOTE), screen()),
        )
        send("Enter")
        wait_for("an individual turn note", lambda: "[Close]" in screen() and NOTE in screen())
        send("Escape")
        wait_for("the restored turn notes view", lambda: axis("2 Turn notes") and "[Close]" not in screen())
        send("Escape")
        wait_for("the overview after leaving turn notes", lambda: "[Back]" not in screen())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(api.base)
                page.get_by_label("Server access token").fill(api.token)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.get_by_role("link", name=PROJECT, exact=True).click()
                page.get_by_text(NOTE, exact=True).wait_for()
                page.get_by_text(PROJECT_NOTE, exact=True).wait_for()
                linked_turn = page.locator(".rowbtn").filter(has_text=TASK).first
                linked_note = page.get_by_role("button", name=NOTE, exact=True)
                linked_turn.focus()
                page.keyboard.press("l")
                assert linked_note.evaluate("(note) => document.activeElement === note")
                page.keyboard.press("h")
                assert linked_turn.evaluate("(turn) => document.activeElement === turn")
                page.get_by_role("button", name="Untitled note", exact=True).wait_for()
                page.get_by_role("button", name=title, exact=True).click()
                page.get_by_role("group", name="Note actions").get_by_role("button", name="Edit").wait_for()
                assert page.locator("#modalTitle").inner_text() == "★ " + title
                assert page.locator("dialog .markdown h2").count() == 0
                page.get_by_role("button", name="Status", exact=True).click()
                options = page.get_by_role("group", name="Note status")
                assert options.get_by_role("button", name="Highlighted", pressed=True).count() == 1
                options.get_by_role("button", name="Low priority").click()
                page.locator('#modalTitle[data-priority="low"]').wait_for()
                note_paragraphs = page.locator("dialog .markdown p")
                note_paragraphs.nth(1).wait_for()
                assert note_paragraphs.all_inner_texts() == ["\n".join(expected_lines[:3]), paragraph]
                page.get_by_role("dialog").get_by_role("button", name="Edit", exact=True).click()
                page.get_by_role("group", name="Editor actions").get_by_role("button", name="Preview").click()
                page.locator("dialog .note-preview h2").filter(has_text=title).wait_for()
                preview_paragraphs = page.locator("dialog .note-preview p")
                preview_paragraphs.nth(1).wait_for()
                assert preview_paragraphs.all_inner_texts() == ["\n".join(expected_lines[:3]), paragraph]
                page.get_by_role("button", name="Close", exact=True).click()
                page.locator(".note").filter(has_text=NOTE).get_by_role(
                    "button", name="Edit", exact=True
                ).click()
                assert page.get_by_label("Note title").count() == 0
                page.get_by_label("Note text").fill("## " + EDIT)
                page.get_by_role("button", name="Save", exact=True).click()
                page.get_by_text("Insert · Saved", exact=True).wait_for()
                page.get_by_role("button", name="Close", exact=True).click()
                page.get_by_role("button", name=EDIT, exact=True).wait_for()
                page.evaluate("""() => {
                    const fetchOriginal = window.fetch.bind(window);
                    window.fetch = (url, options) => {
                        if (!String(url).startsWith('/api/transcript?')) return fetchOriginal(url, options);
                        window.fetch = fetchOriginal;
                        return new Promise((resolve, reject) => {
                            options.signal.addEventListener('abort', () => {
                                window.fixtureTranscriptAborted = true;
                                reject(options.signal.reason);
                            }, { once: true });
                        });
                    };
                }""")
                linked_turn.click()
                page.get_by_text("Loading transcript…", exact=True).wait_for()
                page.get_by_role("button", name="Close", exact=True).click()
                page.wait_for_function("window.fixtureTranscriptAborted === true")
                page.locator(".rowbtn").filter(has_text=TASK).first.click()
                page.locator("dialog").get_by_text(ANSWER, exact=True).wait_for()
                page.get_by_role("button", name="Close", exact=True).click()
                page.get_by_label("Journey", exact=True).uncheck()
                page.locator(".rowbtn").first.wait_for(state="detached")
                seed(api)
                page.get_by_role("link", name="All projects").click()
                page.get_by_role("link", name=FIXTURE, exact=True).click()
                page.get_by_label("Journey", exact=True).check()
                cells = page.locator(".journey-cell")
                cells.filter(has_text="Add a blank-line case").wait_for()
                if cells.locator(".graph-point").count() != len(api.request("board", project=FIXTURE)["turns"]):
                    raise RuntimeError("Each fixture turn needs one graph point")
                first = cells.filter(has_text="Review the sample import")
                next_turn = cells.filter(has_text="Check the setup guide")
                gutter = first.locator(".journey-graph").bounding_box()
                turn = first.locator(".rowbtn").bounding_box()
                if turn["x"] <= gutter["x"] + gutter["width"]:
                    raise RuntimeError("The browser session graph needs space between its border and the turn card")
                border = first.evaluate("""cell => {
                    const card = cell.closest(".board-card");
                    return card.getBoundingClientRect().x +
                        parseFloat(getComputedStyle(card, "::before").left);
                }""")
                if abs(border - turn["x"]) > 1:
                    raise RuntimeError("The browser card border must begin beyond the session graph")
                above = first.locator(".graph-track.outgoing").bounding_box()
                below = next_turn.locator(".graph-track.through").bounding_box()
                if (
                    above["x"] != below["x"]
                    or abs(above["y"] + above["height"] - below["y"]) > 1
                ):
                    raise RuntimeError("A session line has a gap between turns")
                fork = cells.filter(has_text="Try an empty input")
                fork.locator(".graph-fork").wait_for()
                fork.locator(".graph-crossing").wait_for()
                cells.filter(has_text="Add an empty-input test").get_by_text(
                    "Fork of review", exact=True
                ).wait_for()
                card = page.locator(".board-card.paired").filter(has_text="Try the import on a larger fixture")
                card.get_by_role("button", name="Larger fixture", exact=True).wait_for()
                card.get_by_role("button", name="Baseline run", exact=True).wait_for()
                records = page.get_by_role("region", name="Journey and Turn notes")
                project_notes = page.get_by_role("region", name="Project notes", exact=True)
                if not all(pane.evaluate("(p) => p.scrollHeight > p.clientHeight") for pane in (records, project_notes)):
                    raise RuntimeError("Both fixture panes must have scrollable content")
                project_notes.evaluate("(p) => { p.scrollTop = p.scrollHeight; }")
                if not project_notes.evaluate("(p) => p.scrollTop") or records.evaluate("(p) => p.scrollTop"):
                    raise RuntimeError("Project notes must scroll independently of the linked turn cards")
                review = first.locator(".session-id")
                assert review.inner_text() == "review"
                assert review.evaluate("(label) => getComputedStyle(label).color === " +
                    "getComputedStyle(label.closest('.journey-cell').querySelector('.graph-point')).backgroundColor")
                frame_background = "(cell) => getComputedStyle(cell.closest('.board-card')).backgroundImage"
                assert first.evaluate(frame_background) == "none"
                first.locator(".rowbtn").focus()
                review_cards = records.locator('[data-session="review"]')
                assert review_cards.count() > 1
                assert records.locator(".board-card.selected-session").count() == review_cards.count()
                assert records.locator('[data-session="review"].selected-session').count() == review_cards.count()
                background = first.evaluate(frame_background)
                assert background.startswith("linear-gradient(")
                assert first.locator("..").locator(".turn-notes-cell").evaluate(frame_background) == background
                page.keyboard.press("j")
                assert next_turn.locator("..").evaluate("(card) => card.classList.contains('selected-session')")
                assert records.locator('[data-session="review"].selected-session').count() == 0
                assert first.evaluate(frame_background) == "none"
                page.get_by_text("Session filter", exact=True).click()
                session = page.get_by_role("combobox", name="Session")
                session.select_option("review")
                page.get_by_role("button", name="Highlight", exact=True).click()
                page.get_by_role("button", name="Highlight", exact=True, pressed=True).wait_for()
                assert records.locator('[data-session="review"].selected-session').count() == review_cards.count()
                next_turn.locator(".rowbtn").focus()
                assert records.locator('[data-session="docs"].selected-session').count() == 0
                project_scroll = project_notes.evaluate("(p) => p.scrollTop")
                page.get_by_role("button", name="Show only", exact=True).click()
                next_turn.wait_for(state="detached")
                assert records.locator('.board-card:not([data-session="review"])').count() == 0
                assert project_notes.evaluate("(p) => p.scrollTop") == project_scroll
                assert page.get_by_role("button", name="Earlier turns", exact=True).count() == 0
                page.get_by_role("button", name="Clear filter", exact=True).click()
                next_turn.wait_for()
                next_turn.locator(".rowbtn").focus()
                for keys, prompt in (
                    (("g", "g"), "Review archived import case 21"),
                    (("G",), "Summarize import changes"),
                    (("[",), "Review archived import case 20"),
                    (("]",), "Review archived import case 21"),
                    (("<",), "Review archived import case 00"),
                    ((">",), "Summarize import changes"),
                ):
                    for key in keys:
                        page.keyboard.press(key)
                    records.locator(".rowbtn:focus").filter(has_text=prompt).wait_for()
                page.get_by_role("button", name="Earlier turns", exact=True).click()
                cells.filter(has_text="Review archived import case 00").wait_for()
                page.get_by_role("button", name="Newer turns", exact=True).click()
                first.wait_for()
                page.get_by_role("button", name="Earlier project notes", exact=True).click()
                project_notes.get_by_role("button", name="Archived project note 00", exact=True).wait_for()
                page.get_by_role("button", name="Newer project notes", exact=True).click()
                project_notes.get_by_role("button", name="Blank records", exact=True).wait_for()
                stats = subprocess.check_output([cli, "stats"], env=env, text=True)
                if not all(label in stats for label in ("Database", "Last 30 days", PROJECT)):
                    raise RuntimeError("The CLI did not show the server's storage statistics")
                page.get_by_role("link", name="Storage", exact=True).click()
                page.get_by_text("Database file", exact=True).wait_for()
                projects = page.get_by_role("table", name="Storage by project")
                projects.get_by_role("link", name=PROJECT, exact=True).click()
                page.get_by_role("heading", name=PROJECT, exact=True).wait_for()
                largest = page.get_by_role("table", name="Largest turns")
                largest.get_by_role("button", name=SUMMARY, exact=True).click()
                page.get_by_role("dialog").get_by_text(ANSWER, exact=True).wait_for()
                legacy_browser_pages(browser, api, page.context.cookies())
                if errors:
                    raise RuntimeError(f"Browser errors: {errors}")
            finally:
                browser.close()
        wait_for("the edited note in the TUI", lambda: "Keep the regression" in screen())
        send("3", "Enter")
        wait_for("an individual project note", lambda:
                 "[Edit]" in screen() and PROJECT_NOTE in screen())
        send("e")
        wait_for("the project note's Normal mode", lambda: "Normal ·" in screen())
        send("d", "d", "i")
        wait_for("the project note's Insert mode", lambda: "Insert ·" in screen())
        paste(MOUSE_EDIT)
        send("Escape", ":", "w", "q", "Enter")
        wait_for("the project note edited in place", lambda: any(
            note["body"] == MOUSE_EDIT and note["turn"] is None
            for note in api.request("notes", project=PROJECT)
        ))
    except RuntimeError as error:
        raise RuntimeError(f"{error}\nTerminal state:\n{screen()}") from error
    finally:
        stop_tmux(tmux, "journal")


def check_closed_terminal(cli, env):
    # A detached PTY can disappear without delivering SIGHUP to the client.
    with contextlib.ExitStack() as resources:
        master, slave = os.openpty()
        terminal_attributes = termios.tcgetattr(slave)
        terminal_attributes[0] |= termios.IXON
        termios.tcsetattr(slave, termios.TCSANOW, terminal_attributes)
        output = resources.enter_context(os.fdopen(master, "rb", buffering=0))
        resources.callback(os.close, slave)
        terminal = resources.enter_context(child(
            [cli, "tui", "--project", PROJECT],
            stdin=slave, stdout=slave, stderr=slave,
            env=env | {"TERM": "xterm-256color"},
        ))
        deadline = time.monotonic() + 8
        data = bytearray()
        while b"\x1b[?2004h" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                raise RuntimeError("The test client did not enter terminal mode")
            chunk = output.read(4096)
            if not chunk:
                raise RuntimeError("The test client closed its terminal during startup")
            data.extend(chunk)
        if termios.tcgetattr(slave)[0] & termios.IXON:
            raise RuntimeError("Terminal flow control intercepts Ctrl+S in the editor")
        output.close()
        try:
            terminal.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("The TUI stayed alive after its input terminal closed") from error


def smoke(path, binary, channel, cli):
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            f"https://github.com/{PROJECT}.git",
        ],
        check=True,
    )
    settings = {
        "AGENT_JOURNAL_HOME": str(path / "journal"),
        "AGENT_JOURNAL_TOKEN": secrets.token_urlsafe(24),
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID")
    } | settings
    with (
        ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel) as provider,
        child(
            [cli, "serve", "--port", "0"],
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        ) as server,
    ):
        provider.note_summary_started = threading.Event()
        provider.release_note_summary = threading.Event()
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        try:
            line = startup_line(server)
            match = re.fullmatch(r"Journal listening on (.+)\n", line)
            if not match:
                raise RuntimeError("Could not read the local journal server address")
            _, port = ast.literal_eval(match[1])
            settings["AGENT_JOURNAL_URL"] = f"http://127.0.0.1:{port}"
            environment.update(settings)
            with patch.dict(os.environ, settings):
                api = Api()
                api.save()
            fixture_bin = path / "bin"
            fixture_bin.mkdir()
            gh = fixture_bin / "gh"
            gh.write_text(
                '#!/bin/sh\n[ "$*" = '
                + shlex.quote(
                    f"api --hostname github.com repos/{PROJECT} --jq .full_name"
                )
                + " ] || exit 1\nprintf '%s\\n' "
                + shlex.quote(PROJECT)
                + "\n"
            )
            gh.chmod(0o700)

            config = provider_config(f"http://127.0.0.1:{provider.server_port}/v1")
            runtime = path / "codex-state"
            runtime.mkdir()
            rpc = runtime / "app-server-control/app-server-control.sock"
            rpc.parent.mkdir()
            environment["CODEX_HOME"] = str(runtime)
            local_config = config + [
                "-c",
                "sqlite_home=" + json.dumps(str(runtime)),
                "-c",
                "log_dir=" + json.dumps(str(runtime)),
            ]
            for key, value in {
                "env_vars": ["AGENT_JOURNAL_HOME", "CODEX_HOME", "PATH"],
                "default_tools_approval_mode": "approve",
            }.items():
                local_config.extend(
                    ["-c", f"mcp_servers.journal.{key}=" + json.dumps(value)]
                )
            # The summary worker still launches Codex. The wrapper selects the same local model.
            wrapper = path / "codex"
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.join([binary, *config]) + ' "$@"\n'
            )
            wrapper.chmod(0o700)
            codex_env = environment | {
                "PATH": os.pathsep.join((str(path), str(fixture_bin), environment["PATH"])),
                "AGENT_JOURNAL_URL": "",
                "AGENT_JOURNAL_TOKEN": "",
            }
            hooks_file = runtime / "hooks.json"
            unrelated_hook = {"hooks": [{"type": "command", "command": "true"}]}
            hooks_file.write_text(
                json.dumps({"hooks": {"Interrupt": [unrelated_hook]}}),
                encoding="utf-8",
            )
            for _ in range(2):
                subprocess.run([cli, "setup-codex"], env=codex_env, check=True)
            if json.loads(hooks_file.read_text())["hooks"]["Interrupt"] != [unrelated_hook]:
                raise RuntimeError("Setup changed an unrelated Codex hook")

            observer_directory = Path(settings["AGENT_JOURNAL_HOME"])
            observer_sessions = set()

            def stop_observers():
                observer_sessions.update(observer_groups(observer_directory))
                for group in observer_sessions:
                    stop_group(group)

            with contextlib.ExitStack() as processes:
                processes.callback(stop_observers)
                processes.enter_context(
                    child(
                        [
                            binary,
                            "app-server",
                            "--listen",
                            "unix://" + str(rpc),
                            "--disable",
                            "plugins",
                            *local_config,
                        ],
                        cwd=path,
                        env=codex_env,
                    )
                )
                processes.callback(provider.release_note_summary.set)
                wait_for("the Codex socket", rpc.exists)
                with contextlib.closing(Codex(rpc)) as codex:
                    hooks = codex.call(
                        "hooks/list", {"cwds": [str(repo)]}
                    )["data"][0]["hooks"]
                    hooks = [hook for hook in hooks if hook["eventName"] != "interrupt"]
                    if sorted(hook["eventName"] for hook in hooks) != [
                        "sessionEnd", "sessionStart"
                    ]:
                        raise RuntimeError("Expected two Codex lifecycle hooks")
                    if any(hook["trustStatus"] != "untrusted" for hook in hooks):
                        raise RuntimeError("Installer must leave hook trust to Codex")
                    # Mirror the trust operation of /hooks in this isolated Codex home.
                    codex.call(
                        "config/batchWrite",
                        {
                            "edits": [
                                {
                                    "keyPath": "hooks.state",
                                    "value": {
                                        hook["key"]: {"trusted_hash": hook["currentHash"]}
                                        for hook in hooks
                                    },
                                    "mergeStrategy": "upsert",
                                }
                            ],
                            "reloadUserConfig": True,
                        },
                    )
                    options = {
                        "cwd": str(repo),
                        "historyMode": "paginated",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    }
                    registration = codex.call("thread/start", options)["thread"]["id"]
                    servers = codex.call("mcpServerStatus/list", {"limit": 100})["data"]
                    if not any(
                        server["name"] == "journal"
                        and "register_project" in server["tools"]
                        for server in servers
                    ):
                        raise RuntimeError("Codex did not load the journal MCP tools")
                    start_turn(codex, registration, REGISTER_TASK)
                    wait_for(
                        "project registration through Codex",
                        lambda: any(
                            row["project"] == PROJECT for row in api.request("projects")
                        ),
                    )
                    observer_sessions.update(observer_groups(observer_directory))
                    session = codex.call("thread/start", options)["thread"]["id"]
                    turn = start_turn(codex, session, TASK)
                    unknown = path / "unknown"
                    unknown.mkdir()
                    subprocess.run(["git", "-C", unknown, "init", "-q"], check=True)
                    subprocess.run(
                        [
                            "git", "-C", unknown, "remote", "add", "origin",
                            "https://github.com/example/unregistered.git",
                        ],
                        check=True,
                    )
                    unregistered = codex.call(
                        "thread/start", options | {"cwd": str(unknown)}
                    )["thread"]["id"]
                    private_turn = start_turn(codex, unregistered, TASK)

                    def recorded(identifier, **expected):
                        return any(
                            row["turn"] == identifier
                            and all(row[k] == v for k, v in expected.items())
                            for row in api.request("journey", project=PROJECT)
                        )

                    wait_for(
                        "the running turn", lambda: recorded(turn, status="inProgress")
                    )
                    wait_for(
                        "the separate Codex summary",
                        lambda: recorded(turn, status="completed", summary=SUMMARY),
                    )
                    marker_dir = Path(settings["AGENT_JOURNAL_HOME"]) / "codex-sessions"
                    wait_for(
                        "three Codex session markers",
                        lambda: len(list(marker_dir.glob("session-*"))) == 3,
                    )
                    codex.call("thread/archive", {"threadId": registration})
                    codex.call("thread/archive", {"threadId": unregistered})
                    wait_for(
                        "one remaining session marker",
                        lambda: len(list(marker_dir.glob("session-*"))) == 1,
                    )
                    if recorded(private_turn) or any(
                        row["project"] == "example/unregistered" for row in api.request("projects")
                    ):
                        raise RuntimeError("An unregistered project reached the journal")
                    provider.target = {"thread": session, "turn": turn}
                    note_turn = start_turn(codex, session, NOTE_TASK)
                    wait_for(
                        "the note request turn",
                        lambda: recorded(note_turn, status="completed"),
                    )

                    notes = api.request("notes", project=PROJECT)
                    if (
                        len(notes) != 1
                        or notes[0]["turn"] != turn
                        or notes[0]["body"] != "# " + NOTE
                    ):
                        raise RuntimeError(
                            "The Codex MCP note did not land on the previous turn"
                        )
                    tools = api.request(
                        "transcript", project=PROJECT, thread=session, turn=note_turn
                    )
                    calls = [
                        (item["tool"], item["status"])
                        for item in tools
                        if item["type"] == "mcpToolCall"
                    ]
                    if calls != [
                        ("read_journal", "completed"),
                        ("add_note", "completed"),
                    ]:
                        raise RuntimeError(f"Codex MCP calls did not complete: {calls}")
                    wait_for("the final summary to start", provider.note_summary_started.is_set)
                    codex.call("thread/archive", {"threadId": session})
                    wait_for(
                        "the last Codex session to close",
                        lambda: not any(marker_dir.glob("session-*")),
                    )
                    provider.release_note_summary.set()
                    wait_for(
                        "the closed session's final summary",
                        lambda: recorded(note_turn, status="completed", summary=NOTE_SUMMARY),
                    )
                    cache = marker_dir.parent / f"observer-{api.cache_key}.sqlite3"

                    def collector_exited():
                        lock = collector_lock(cache)
                        if lock is None:
                            return False
                        lock.close()
                        return True

                    wait_for("the collector to stop after the last session", collector_exited)
                check_clients(path, repo, cli, environment, api, channel)
                check_closed_terminal(cli, environment)
        finally:
            provider.shutdown()
            provider_thread.join(timeout=5)
            if provider_thread.is_alive():
                raise RuntimeError("The test model server did not stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex", default="codex", help="Codex CLI binary or command name"
    )
    parser.add_argument(
        "--browser-channel", choices=("chrome",), help="Use an installed Chrome"
    )
    parser.add_argument(
        "--client",
        type=Path,
        default=Path(sys.executable).with_name("agent-journal"),
        help="Agent Journal executable to exercise",
    )
    args = parser.parse_args()
    binary = shutil.which(args.codex)
    if not binary or not shutil.which("tmux") or not shutil.which("git"):
        parser.error("Install Codex CLI, tmux, and Git before running the smoke test")
    with cleanup_on_termination(), test_workspace("aj-smoke-") as directory:
        smoke(directory, binary, args.browser_channel, args.client.resolve())
    print("Local Codex, summary, observer, MCP, TUI, and browser smoke test passed.")


if __name__ == "__main__":
    main()
