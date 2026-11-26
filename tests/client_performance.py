"""Repeat the synthetic terminal, network, and browser performance workloads."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import closing
from queue import Queue
from threading import Thread
from unittest.mock import patch

from agent_journal.common import Api
from agent_journal.server import JournalHandler, connect, make_server
from agent_journal.tui import Item, detail_lines

from tests.smoke_local import wait_for
from tests.processes import cleanup_on_termination, stop_tmux
from tests.workspace import test_workspace


PROJECT = "example/performance-fixture"
THREAD = "fixture-thread"
TOKEN = "synthetic-performance-token"


def seed(server):
    now = int(time.time())
    with closing(connect(server.database)) as db, db:
        db.execute("INSERT INTO projects VALUES (?, ?, 1)", (PROJECT, now - 5000))
        db.executemany(
            """INSERT INTO turns(project,thread,turn,started,status,prompt,summary,items,revision)
               VALUES (?, ?, ?, ?, 'completed', ?, 'Synthetic result', '[]', 1)""",
            [
                (
                    PROJECT,
                    THREAD,
                    f"turn-{i:03d}",
                    now - 2000 + i,
                    f"Review synthetic fixture {i:03d}",
                )
                for i in range(120)
            ],
        )
        body = ("Synthetic fixture note. " * 100)[:2048]
        db.executemany(
            "INSERT INTO notes(id,project,thread,turn,body,created) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"note-{i}", PROJECT, THREAD, f"turn-{i % 60:03d}", body, now)
                for i in range(940)
            ]
            + [
                (f"current-{i}", PROJECT, THREAD, f"turn-{i:03d}", body, now)
                for i in range(60, 120)
            ]
            + [(f"project-{i}", PROJECT, None, None, body, now) for i in range(10)],
        )


def check_page_size(api):
    data = api.request("board", project=PROJECT)
    assert len(data["turns"]) == 60
    assert len(data["turn_notes"]) == 60
    assert len(data["project_notes"]) == 10
    assert all(
        "body" not in item for item in data["turn_notes"] + data["project_notes"]
    )
    raw = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    assert raw < 60_000, raw
    assert api.request("board", project=PROJECT, revision=data["revision"])["unchanged"]
    print(f"Scoped board: {raw:,} bytes for 60 turns and 70 note previews", flush=True)


def check_terminal(path, base, requests, set_offline, api):
    tmux = ["tmux", "-S", str(path / "tm")]
    command = shlex.join(
        [sys.executable, "-m", "agent_journal", "tui", "--project", PROJECT]
    )
    client_home = str(path / "tui")
    env = os.environ | {
        "AGENT_JOURNAL_HOME": client_home,
        "AGENT_JOURNAL_URL": base,
        "AGENT_JOURNAL_TOKEN": TOKEN,
        "TERM": "xterm-256color",
    }
    target = tmux + ["send-keys", "-t", "performance"]

    def capture():
        return subprocess.check_output(
            tmux + ["capture-pane", "-pt", "performance"], text=True
        )

    def launch():
        subprocess.run(
            tmux
            + [
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "performance",
                "-x",
                "125",
                "-y",
                "30",
                command,
            ],
            env=env,
            check=True,
            timeout=10,
        )

    try:
        launch()
        wait_for(
            "the TUI to show its first page",
            lambda: "Review synthetic fixture" in capture(),
            timeout=5,
        )
        requests.get(timeout=2)
        requests.get(timeout=5)
        start = time.monotonic()
        subprocess.run(target + ["2"], check=True)
        while "Axes · 2 Turn notes" not in capture():
            assert time.monotonic() - start < 0.100, capture()
            time.sleep(0.005)
        elapsed = time.monotonic() - start
        assert elapsed < 0.100, elapsed
        print(f"Axis switch during 350 ms poll: {elapsed * 1000:.1f} ms", flush=True)
        set_offline(True)
        subprocess.run(target + ["n"], check=True)
        wait_for(
            "an offline TUI note editor",
            lambda: "Insert ·" in capture(),
            timeout=5,
        )
        title = "TUI 오프라인 초안"
        subprocess.run(target + ["-l", f"# {title}"], check=True)
        subprocess.run(target + ["Enter", "Enter"], check=True)
        subprocess.run(target + ["-l", "  합성 초안"], check=True)
        subprocess.run(target + ["Escape", ":", "w", "q", "Enter"], check=True)
        wait_for(
            "the offline note window to close",
            lambda: "[Close]" not in capture(),
            timeout=5,
        )
        subprocess.run(target + ["q"], check=True)
        wait_for(
            "the first TUI to exit",
            lambda: (
                subprocess.run(
                    tmux + ["has-session", "-t", "performance"], capture_output=True
                ).returncode
                != 0
            ),
            timeout=5,
        )
        launch()
        wait_for(
            "the cached page after an offline TUI restart",
            lambda: "Review synthetic fixture" in capture(),
            timeout=5,
        )
        set_offline(False)
        wait_for(
            "the reopened TUI to sync its saved draft",
            lambda: any(
                note["title"] == title and note["body"] == f"# {title}\n\n  합성 초안"
                for note in api.request("notes", project=PROJECT)
            ),
            timeout=8,
        )
        print(
            "TUI restarted offline with cached data and synced its local draft.",
            flush=True,
        )
        subprocess.run(target + ["m"], check=True)
        wait_for("a turn note editor", lambda: "Insert ·" in capture(), timeout=5)
        subprocess.run(target + ["-l", "# Move terminal fixture"], check=True)
        subprocess.run(target + ["Enter", "Enter"], check=True)
        subprocess.run(target + ["-l", "Keep the terminal text."], check=True)
        subprocess.run(target + ["Escape", ":", "w", "q", "Enter"], check=True)
        wait_for(
            "the source turn note",
            lambda: any(
                note["title"] == "Move terminal fixture" and note["turn"] == "turn-119"
                for note in api.request("notes", project=PROJECT)
            ),
            timeout=8,
        )
        subprocess.run(target + [">"], check=True)
        wait_for(
            "the terminal note selected",
            lambda: "› Move terminal fixture" in capture(),
            timeout=8,
        )
        subprocess.run(target + ["Enter"], check=True)
        wait_for("the terminal note detail", lambda: "[Cut]" in capture(), timeout=5)
        subprocess.run(target + ["x", "Escape", "1"], check=True)
        for keys, turn in (
            (["g", "g"], "060"), (["G"], "119"),
            (["["], "059"), (["]"], "060"),
            (["<"], "000"), ([">"], "119"), (["<"], "000"),
        ):
            subprocess.run(target + keys, check=True)
            wait_for(
                f"Journey turn {turn} selected by {''.join(keys)}",
                lambda: any(
                    ">" in line[:8] and f"fixture {turn}" in line
                    for line in capture().splitlines()
                ),
                timeout=8,
            )
        subprocess.run(target + ["p"], check=True)
        wait_for(
            "the terminal note to move across pages",
            lambda: any(
                note["title"] == "Move terminal fixture" and note["turn"] == "turn-000"
                for note in api.request("notes", project=PROJECT)
            ),
            timeout=8,
        )
        subprocess.run(target + ["2", "Tab", "Enter"], check=True)
        wait_for(
            "the moved terminal note detail",
            lambda: "[Delete]" in capture() and "Move terminal fixture" in capture(),
            timeout=8,
        )
        subprocess.run(target + ["D", "y"], check=True)
        wait_for(
            "terminal note deletion",
            lambda: (
                not any(
                    note["title"] == "Move terminal fixture"
                    for note in api.request("notes", project=PROJECT)
                )
            ),
            timeout=8,
        )
        print("TUI moved a note across pages and confirmed its deletion.", flush=True)
    finally:
        stop_tmux(tmux, "performance")


def check_browser(base, channel, api):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        try:
            page = browser.new_page()
            assert page.request.post(base + "/api/login", data={"token": TOKEN}).ok
            start = time.monotonic()
            page.goto(base + "/#example%2Fperformance-fixture")
            page.get_by_role("heading", name=PROJECT, exact=True).wait_for(timeout=6000)
            elapsed = time.monotonic() - start
            print(f"Browser rendered the 3.3 s response in {elapsed:.2f} s", flush=True)
            page.wait_for_function("navigator.serviceWorker.controller !== null")
            note_title = "한글 초안"
            contents = f"{note_title}\n===\n\n```text\n    indented 👩‍💻\n```\n"
            page.context.set_offline(True)
            page.get_by_role("button", name="＋", exact=True).click()
            page.get_by_label("Note text").fill(contents)
            page.get_by_role("button", name="Preview", exact=True).click()
            assert (
                page.locator(".note-preview code").inner_text() == "    indented 👩‍💻"
            )
            page.get_by_role("button", name="Close", exact=True).click()
            page.reload()
            page.get_by_role("button", name="한글 초안", exact=True).click()
            assert page.get_by_label("Note text").input_value() == contents
            page.context.set_offline(False)
            page.get_by_text("Insert · Saved", exact=True).wait_for(timeout=6000)
            notes = api.request("notes", project=PROJECT)
            assert any(
                note["title"] == note_title and note["body"] == contents
                for note in notes
            )
            print(
                "Browser reloaded an offline Unicode draft and synced it after reconnecting.",
                flush=True,
            )
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="+ Note", exact=True).first.click()
            page.get_by_label("Note text").fill("# Movable fixture\n\nTrack across turns")
            page.get_by_text("Insert · Saved", exact=True).wait_for(timeout=6000)
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="Movable fixture", exact=True).click()
            page.get_by_role("button", name="Cut", exact=True).click()
            page.get_by_role("button", name="Paste note", exact=True).last.click()
            wait_for(
                "the web note to move to the last turn",
                lambda: any(
                    note["title"] == "Movable fixture" and note["turn"] == "turn-119"
                    for note in api.request("notes", project=PROJECT)
                ),
                timeout=8,
            )
            page.get_by_role("button", name="Movable fixture", exact=True).click(
                timeout=8000
            )
            page.get_by_role("button", name="Delete", exact=True).click()
            page.get_by_role("button", name="Delete note", exact=True).click()
            wait_for(
                "the moved note to be deleted",
                lambda: (
                    not any(
                        note["title"] == "Movable fixture"
                        for note in api.request("notes", project=PROJECT)
                    )
                ),
                timeout=8,
            )
            print("Browser moved a turn note and confirmed its deletion.", flush=True)
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-channel", choices=("chrome",))
    args = parser.parse_args()
    with cleanup_on_termination(), test_workspace("aj-performance-") as path:
        with (
            patch.dict(os.environ, {"AGENT_JOURNAL_HOME": str(path)}),
            make_server("127.0.0.1", 0, path / "server.sqlite3", TOKEN) as server,
        ):
            seed(server)
            base = f"http://127.0.0.1:{server.server_port}"
            queue = Queue()
            delay = 0
            offline = False
            dispatch = JournalHandler.dispatch

            def set_offline(value):
                nonlocal offline
                offline = value

            def delayed(handler):
                if offline and handler.path.startswith("/api/"):
                    return handler.reply(503, {"error": "Synthetic offline state"})
                if handler.path.split("?")[0] == "/api/board":
                    queue.put(None)
                    time.sleep(delay)
                dispatch(handler)

            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = Api(base, TOKEN)
                check_page_size(api)
                with patch.object(JournalHandler, "dispatch", delayed):
                    delay = 0.35
                    check_terminal(path, base, queue, set_offline, api)
                    delay = 3.3
                    check_browser(base, args.browser_channel, api)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("The performance fixture server did not stop")
        command = ("Synthetic command output for a long transcript.\n" * 120_000)[
            :5_000_000
        ]
        item = Item("Transcript", content=command)
        start = time.monotonic()
        assert len(detail_lines(item, 118, 25)) == 25 and item.wrapping is not None
        print(
            f"First viewport of 5 MB: {(time.monotonic() - start) * 1000:.2f} ms to format"
        )


if __name__ == "__main__":
    main()
