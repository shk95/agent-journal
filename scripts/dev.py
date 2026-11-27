"""Run an isolated loopback journal for local development."""

import argparse
import math
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "example/journal-fixture"


def token(filepath):
    try:
        fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
    return filepath.read_text(encoding="utf-8").strip()


def seed(api):
    api.request("register", {"project": FIXTURE})
    project = next(row for row in api.request("projects") if row["project"] == FIXTURE)
    samples = [
        ("review", "01", None, "Review the sample import", "Imported the sample rows"),
        ("docs", "04", None, "Check the setup guide", "Found an outdated command"),
        ("review", "02", None, "Try an empty input", "Empty input returns no rows"),
        (
            "tests",
            "03",
            ("review", "02"),
            "Add an empty-input test",
            "Regression test passes",
        ),
        ("docs", "05", None, "Update the setup command", "Verified the new example"),
        (
            "review",
            "06",
            None,
            "Review malformed rows",
            "Invalid rows include a reason",
        ),
        (
            "tests",
            "07",
            ("review", "02"),
            "Add a blank-line case",
            "Blank lines are ignored",
        ),
        (
            "review",
            "08",
            None,
            "Inspect a file with mixed line endings",
            "Normalized line endings",
        ),
        ("docs", "09", None, "Document the input format", "Added a delimiter example"),
        (
            "review",
            "10",
            None,
            "Read a quoted field",
            "The delimiter stays in the field",
        ),
        (
            "tests",
            "11",
            ("review", "10"),
            "Cover quoted delimiters",
            "Both escape cases pass",
        ),
        (
            "docs",
            "12",
            None,
            "Describe malformed rows",
            "Documented the rejection message",
        ),
        (
            "review",
            "13",
            None,
            "Review duplicate headers",
            "Duplicate names are rejected",
        ),
        (
            "tests",
            "14",
            None,
            "Add a duplicate-header fixture",
            "The test names the header",
        ),
        (
            "docs",
            "15",
            None,
            "Check the quickstart example",
            "The copyable command works",
        ),
        (
            "review",
            "16",
            None,
            "Check a multilingual sample",
            "Preserved 한글 and accents",
        ),
        (
            "tests",
            "17",
            ("review", "16"),
            "Verify Unicode round-tripping",
            "Stored and reopened the same text",
        ),
        (
            "docs",
            "18",
            None,
            "Explain the output fields",
            "Added an expected output example",
        ),
        (
            "review",
            "19",
            None,
            "Try the import on a larger fixture",
            "All valid rows imported",
        ),
        (
            "tests",
            "20",
            None,
            "Check the final regression suite",
            "Import fixtures all pass",
        ),
        (
            "review",
            "21",
            None,
            "Summarize import changes",
            "Prepared the release notes",
        ),
    ]
    archived = [
        ("archive", f"archive-{i:02}", None, f"Review archived import case {i:02}", "Recorded the import result")
        for i in range(60)
    ]
    for index, (thread, turn, parent, prompt, summary) in enumerate(archived + samples):
        parent_thread, parent_turn = parent or (None, None)
        api.request(
            "turns",
            {
                "project": FIXTURE,
                "thread": thread,
                "turn": turn,
                "parent": parent_thread,
                "parent_turn": parent_turn,
                "started": math.ceil(project["since"]) + index,
                "prompt": prompt,
                "summary": summary,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": prompt}],
                    },
                    {"type": "agentMessage", "text": summary},
                ],
                "status": "completed",
                "revision": time.time_ns() // 1000,
            },
        )
    for note in (
        *(
            {
                "id": f"fixture-archive-note-{i:02}",
                "thread": "archive",
                "turn": f"archive-{i:02}",
                "body": f"# Archived case {i:02}\n\nKeep the expected output with the example.",
            }
            for i in range(0, 60, 10)
        ),
        *(
            {
                "id": f"fixture-archived-project-note-{i:02}",
                "body": f"# Archived project note {i:02}\n\nRetain the corresponding sample result.",
            }
            for i in range(60)
        ),
        {
            "id": "fixture-turn-note",
            "thread": "review",
            "turn": "02",
            "body": "# Blank input\n\nCheck blank lines too.",
            "priority": "highlighted",
        },
        {
            "id": "fixture-turn-note-quoted",
            "thread": "review",
            "turn": "10",
            "body": "# Quoted field\n\nKeep a delimiter inside a quoted field.",
            "priority": "low",
        },
        *(
            {
                "id": f"fixture-turn-note-{index}",
                "thread": thread,
                "turn": turn,
                "body": f"# {title}\n\n{body}",
            }
            for index, (thread, turn, title, body) in enumerate(
                (
                    (
                        "review",
                        "01",
                        "Sample input",
                        "Use a small synthetic input file.",
                    ),
                    (
                        "tests",
                        "03",
                        "Empty input fixture",
                        "An empty file is a supported input.",
                    ),
                    ("docs", "05", "Setup command", "Copy the command from the guide."),
                    (
                        "review",
                        "06",
                        "Rejection message",
                        "Include an actionable reason.",
                    ),
                    ("tests", "07", "Blank lines", "Include repeated blank lines."),
                    (
                        "review",
                        "08",
                        "Mixed line endings",
                        "Test both newline styles in one file.",
                    ),
                    (
                        "docs",
                        "09",
                        "Input delimiter",
                        "Give a short example in the guide.",
                    ),
                    (
                        "tests",
                        "11",
                        "Escaped quotes",
                        "Include a field with an escaped quote.",
                    ),
                    (
                        "review",
                        "13",
                        "Header uniqueness",
                        "Check the reported header name.",
                    ),
                    ("docs", "15", "Copyable quickstart", "Keep the path relative."),
                    (
                        "review",
                        "16",
                        "한글 샘플",
                        "Include a decomposed and a composed example.",
                    ),
                    (
                        "tests",
                        "17",
                        "Unicode stored exactly",
                        "Compare the bytes after import.",
                    ),
                    (
                        "review",
                        "19",
                        "Larger fixture",
                        "Use generated rows to measure the import.",
                    ),
                    ("review", "19", "Baseline run", "Compare the final row count."),
                    ("review", "21", "Release note", "Mention the error messages."),
                    (
                        "review",
                        "21",
                        "Regression coverage",
                        "Link the synthetic fixtures.",
                    ),
                )
            )
        ),
        {"id": "fixture-project-note", "body": "Keep the import examples short."},
        *(
            {
                "id": f"fixture-project-note-{index}",
                "body": f"# {title}\n\n{body}",
                "priority": {"Unicode samples": "highlighted", "Blank records": "low"}.get(title, "normal"),
            }
            for index, (title, body) in enumerate(
                (
                    ("Example data", "Use generated inputs in the guide."),
                    ("Input delimiter", "Document comma-separated input."),
                    ("Filename examples", "Use portable relative paths."),
                    ("Expected output", "Show two successful rows."),
                    ("Error messages", "Include the rejected row and a reason."),
                    ("Blank records", "Define which blank rows are skipped."),
                    ("Escaping", "Show a value with quotes."),
                    ("Duplicate headers", "Require unique column names."),
                    ("Line endings", "Support the common line ending conventions."),
                    ("Unicode samples", "Keep 한글 and accented text in the fixtures."),
                    ("Quickstart", "Use a single copyable command."),
                    ("Regression cases", "Keep examples with the test suite."),
                    ("Release checklist", "Check imports and the guide together."),
                    ("Follow-up", "Review how rejected rows are reported."),
                )
            )
        ),
    ):
        api.request("notes", {"project": FIXTURE, **note})


def demo(client, port):
    from agent_journal.common import Api
    from agent_journal.server import JournalHandler, make_server

    class FixtureWebHandler(JournalHandler):
        def browser_authenticated(self, cookie, session):
            return True

    access = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="agent-journal-demo-") as folder:
        database = Path(folder) / "server.sqlite3"
        handler = FixtureWebHandler if client == "web" else JournalHandler
        with make_server("127.0.0.1", port, database, access, handler=handler) as server:
            server.browser_cookie = f"journal_demo_{server.server_port}"
            base = f"http://127.0.0.1:{server.server_port}"
            os.environ.update(
                AGENT_JOURNAL_HOME=folder,
                AGENT_JOURNAL_TOKEN=access,
                AGENT_JOURNAL_URL=base,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = Api()
                seed(api)
                if client == "tui":
                    from agent_journal.tui import run

                    run(api, FIXTURE)
                else:
                    import webbrowser
                    from urllib.parse import quote

                    url = base + "/#" + quote(FIXTURE, safe="")
                    print(f"Fixture journal: {url}")
                    print("Press Ctrl+C to stop the demo.", flush=True)
                    webbrowser.open_new_tab(url)
                    threading.Event().wait()
            finally:
                server.shutdown()
                thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        help="loopback port (default: automatic for demo, 8766 otherwise)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("AGENT_JOURNAL_DEV_HOME", ROOT / ".local/dev")),
        help="state directory for the persistent development server",
    )
    parser.add_argument("command", choices=("demo", "serve", "seed", "token", "client"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.command == "demo":
        client = args.args or ["tui"]
        if client not in (["tui"], ["web"]):
            parser.error("use demo [tui|web]")
        try:
            demo(client[0], args.port or 0)
        except KeyboardInterrupt:
            pass
        except (OSError, RuntimeError, ValueError) as error:
            parser.exit(1, f"Could not run fixture journal: {error}\n")
        return 0
    port = 8766 if args.port is None else args.port
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "AGENT_JOURNAL_TOKEN": token(args.state / "token"),
        "AGENT_JOURNAL_URL": f"http://127.0.0.1:{port}",
        "AGENT_JOURNAL_HOME": str(args.state.resolve()),
    }
    os.environ.update(env)
    if args.command == "seed":
        from agent_journal.common import Api

        seed(Api())
        print(FIXTURE)
    elif args.command == "token":
        print(env["AGENT_JOURNAL_TOKEN"])
    else:
        cli = ["serve", "--port", str(port)] if args.command == "serve" else args.args
        executable = Path(sys.executable).with_name("agent-journal")
        os.execv(executable, [str(executable), *cli])
    return 0


if __name__ == "__main__":
    sys.exit(main())
