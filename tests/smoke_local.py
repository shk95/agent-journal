"""Exercise a real Codex App Server and the journal clients on loopback."""

import argparse
import ast
import contextlib
import curses
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_journal.common import Api
from agent_journal.observer import Codex
from scripts.dev import FIXTURE, seed

PROJECT = "example/journal-smoke"
REGISTER_TASK = "Register this repository with Agent Journal."
TASK = "Verify the sample import."
NOTE_TASK = "Attach a note to the previous result."
ANSWER = "The sample import passed."
SUMMARY = "Verified the sample import."
NOTE = "Keep a regression case."
EDIT = "Keep the regression fixture with the import."
PROJECT_NOTE = "Review other import formats later."
MOUSE_NOTE = "Keep the input in the example."
MOUSE_EDIT = "Save the input with its example."


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
            answer = SUMMARY if TASK in serialized else "Saved the requested turn note."
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
                    "body": NOTE,
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


@contextlib.contextmanager
def child(command, **options):
    proc = subprocess.Popen(command, **options)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


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
    editor = path / "editor"
    editor.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(MOUSE_EDIT) + ' > "$1"\n'
    )
    editor.chmod(0o700)
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
            shlex.quote(str(cli)),
        ],
        check=True,
        env=env | {"TERM": "xterm-256color", "VISUAL": str(editor)},
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

    def click_label(label):
        y, line = next(
            (i, row) for i, row in enumerate(screen().splitlines()) if label in row
        )
        mouse(0, line.index(label), y, click=True)

    def selected(prompt):
        return any(
            row.startswith(">") and prompt in row for row in screen().splitlines()
        )

    try:
        wait_for("the TUI journey for the current checkout", lambda: TASK in screen())
        if screen().splitlines()[0] != PROJECT:
            raise RuntimeError("The default TUI did not select the checkout's project")
        target = tmux + ["send-keys", "-t", "journal"]
        turn_row = next(i for i, row in enumerate(screen().splitlines()) if TASK in row)
        lines = screen().splitlines()
        if [line[1] for line in lines[turn_row : turn_row + 3]] != ["●", "│", "●"]:
            raise RuntimeError("The terminal session line stops at the summary row")
        click_label("[1 Journey]")
        wait_for(
            "the Journey axis view",
            lambda: (
                screen().splitlines()[1] == "[Back] Axes · 1 Journey"
                and screen().splitlines()[3] == "Journey"
            ),
        )
        click_label("[2 Turn notes]")
        wait_for(
            "the combined axis view",
            lambda: (
                "Axes · 1 Journey + 2 Turn notes" in screen().splitlines()[1]
                and "Turn notes" in screen().splitlines()[3]
            ),
        )
        subprocess.run(target + ["2"], check=True)
        wait_for("the single axis again", lambda: screen().splitlines()[3] == "Journey")
        subprocess.run(target + ["2"], check=True)
        wait_for(
            "the restored combined axis view",
            lambda: "Turn notes" in screen().splitlines()[3],
        )
        turn_row = next(i for i, row in enumerate(screen().splitlines()) if TASK in row)
        mouse(0, 1, turn_row, click=True)
        wait_for("the selected turn", lambda: selected(TASK))
        click_label("[Transcript]")
        wait_for(
            "the clicked turn's transcript with its parent context",
            lambda: (
                ANSWER in screen()
                and "Axes · 1 Journey + 2 Turn notes" in screen().splitlines()[1]
                and f"[Close] Transcript · {TASK}" in screen().splitlines()[2]
            ),
        )
        subprocess.run(target + ["3"], check=True)
        click_label("[Close]")
        wait_for(
            "the restored axes and selected turn",
            lambda: (
                "[Back] Axes · 1 Journey + 2 Turn notes" == screen().splitlines()[1]
                and selected(TASK)
            ),
        )
        if hasattr(curses, "BUTTON5_PRESSED"):
            mouse(65, 1, turn_row)
            wait_for("the turn after scrolling down", lambda: selected(NOTE_TASK))
            click_label("[Transcript]")
            wait_for(
                "the open transcript after scrolling down",
                lambda: NOTE_TASK in screen() and "[Close]" in screen(),
            )
            mouse(65, 1, 3)
            wait_for(
                "the transcript scrolled down",
                lambda: screen().splitlines()[3].strip() == "{",
            )
            mouse(64, 1, 3)
            wait_for(
                "the transcript scrolled back",
                lambda: screen().splitlines()[3].strip() == "[userMessage]",
            )
            click_label("[Close]")
            wait_for(
                "the journey after scrolling down", lambda: "[Transcript]" in screen()
            )
            mouse(64, 1, turn_row)
            wait_for("the turn after scrolling up", lambda: selected(TASK))
        click_label("[+ Turn note]")
        wait_for(
            "the clicked turn note input", lambda: "Empty line cancels" in screen()
        )
        subprocess.run(target + ["-l", MOUSE_NOTE], check=True)
        subprocess.run(target + ["Enter"], check=True)
        first_turn = api.request("journey", project=PROJECT)[0]["turn"]
        wait_for(
            "the clicked turn's saved note",
            lambda: any(
                note["turn"] == first_turn and note["body"] == MOUSE_NOTE
                for note in api.request("notes", project=PROJECT)
            ),
        )
        click_label("[Back]")
        wait_for(
            "the overview and its original turn selection",
            lambda: "Project notes" in screen().splitlines()[2] and selected(NOTE_TASK),
        )
        subprocess.run(target + ["n"], check=True)
        wait_for("the TUI note input", lambda: "Empty line cancels" in screen())
        subprocess.run(target + ["-l", PROJECT_NOTE], check=True)
        subprocess.run(target + ["Enter"], check=True)
        wait_for(
            "the saved TUI note",
            lambda: any(
                note["body"] == PROJECT_NOTE and note["turn"] is None
                for note in api.request("notes", project=PROJECT)
            ),
        )
        subprocess.run(target + ["2"], check=True)
        wait_for(
            "the turn notes axis",
            lambda: screen().splitlines()[1] == "[Back] Axes · 2 Turn notes",
        )
        subprocess.run(target + ["Enter"], check=True)
        wait_for(
            "an individual turn note",
            lambda: (
                f"[Close] Turn note · {TASK}" in screen().splitlines()[2]
                and NOTE in screen()
            ),
        )
        subprocess.run(target + ["Escape"], check=True)
        wait_for(
            "the returned turn notes view",
            lambda: screen().splitlines()[1] == "[Back] Axes · 2 Turn notes",
        )
        subprocess.run(target + ["Escape"], check=True)
        wait_for(
            "the overview after leaving turn notes",
            lambda: "Project notes" in screen().splitlines()[2],
        )
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
                page.locator(".note").filter(has_text=NOTE).get_by_role(
                    "button", name="Edit", exact=True
                ).click()
                page.get_by_label("Note text").fill(EDIT)
                page.get_by_role("button", name="Save", exact=True).click()
                page.get_by_text(EDIT, exact=True).wait_for()
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
                if cells.locator(".graph-point").count() != 7:
                    raise RuntimeError("Each fixture turn needs one graph point")
                first = cells.filter(has_text="Review the sample import")
                next_turn = cells.filter(has_text="Check the setup guide")
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
                if errors:
                    raise RuntimeError(f"Browser errors: {errors}")
            finally:
                browser.close()
        wait_for(
            "the edited note in the TUI", lambda: "Keep the regression" in screen()
        )
        # The note display is clipped to the column width, but the whole row opens it.
        mouse(0, 110, 3, click=True)
        wait_for(
            "an individual project note",
            lambda: (
                "[Close] Project note" in screen().splitlines()[2]
                and PROJECT_NOTE in screen()
            ),
        )
        subprocess.run(target + ["e"], check=True)
        wait_for(
            "the project note edited by mouse",
            lambda: any(
                note["body"] == MOUSE_EDIT and note["turn"] is None
                for note in api.request("notes", project=PROJECT)
            ),
        )
        click_label("[Close]")
        wait_for(
            "the returned project notes view",
            lambda: screen().splitlines()[1] == "[Back] Axes · 3 Project notes",
        )
        subprocess.run(target + ["q"], check=True)
    finally:
        subprocess.run(tmux + ["kill-server"], check=False, capture_output=True)


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
    rpc = path / "rpc"
    with (
        ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel) as provider,
        child(
            [cli, "serve", "--port", "0"],
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        ) as server,
    ):
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        try:
            line = server.stdout.readline()
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
            environment["CODEX_HOME"] = str(runtime)
            local_config = config + [
                "-c",
                "sqlite_home=" + json.dumps(str(runtime)),
                "-c",
                "log_dir=" + json.dumps(str(runtime)),
            ]
            mcp_args = ["mcp", "--socket", str(rpc)]
            for key, value in {
                "command": str(cli),
                "args": mcp_args,
                "env_vars": ["AGENT_JOURNAL_HOME", "PATH"],
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
            observer_env = environment | {
                "PATH": str(path) + os.pathsep + environment["PATH"]
            }

            with contextlib.ExitStack() as processes:
                processes.enter_context(
                    child(
                        [
                            binary,
                            "app-server",
                            "--listen",
                            "unix://" + str(rpc),
                            "--disable",
                            "hooks",
                            "--disable",
                            "plugins",
                            *local_config,
                        ],
                        cwd=path,
                        env=environment
                        | {
                            "PATH": str(fixture_bin) + os.pathsep + environment["PATH"],
                            "AGENT_JOURNAL_URL": "",
                            "AGENT_JOURNAL_TOKEN": "",
                        },
                    )
                )
                wait_for("the Codex socket", rpc.exists)
                processes.enter_context(
                    child(
                        [cli, "observe", "--socket", rpc, "--interval", "0.35"],
                        cwd=repo,
                        env=observer_env,
                    )
                )
                with contextlib.closing(Codex(rpc)) as codex:
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
                    session = codex.call("thread/start", options)["thread"]["id"]
                    turn = start_turn(codex, session, TASK)

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
                        or notes[0]["body"] != NOTE
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
                check_clients(path, repo, cli, environment, api, channel)
        finally:
            provider.shutdown()


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
    with tempfile.TemporaryDirectory(prefix="aj-smoke-", dir="/tmp") as directory:
        smoke(Path(directory), binary, args.browser_channel, args.client.resolve())
    print("Local Codex, summary, observer, MCP, TUI, and browser smoke test passed.")


if __name__ == "__main__":
    main()
