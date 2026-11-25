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
        ("review", "02", None, "Try an empty input", "Empty input returns no rows"),
        ("tests", "03", "review", "Add an empty-input test", "Regression test passes"),
    ]
    for index, (thread, turn, parent, prompt, summary) in enumerate(samples):
        api.request(
            "turns",
            {
                "project": FIXTURE,
                "thread": thread,
                "turn": turn,
                "parent": parent,
                "parent_turn": "01" if parent else None,
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
        {
            "id": "fixture-turn-note",
            "thread": "review",
            "turn": "02",
            "body": "Check blank lines too.",
        },
        {"id": "fixture-project-note", "body": "Keep the import examples short."},
    ):
        api.request("notes", {"project": FIXTURE, **note})


def demo(client, port):
    from agent_journal.common import Api
    from agent_journal.server import make_server

    access = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="agent-journal-demo-") as folder:
        database = Path(folder) / "server.sqlite3"
        with make_server("127.0.0.1", port, database, access) as server:
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
                    print(f"Temporary access token: {access}")
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
