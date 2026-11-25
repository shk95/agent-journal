"""Run an isolated loopback journal for local development."""

import argparse
import math
import os
import secrets
import subprocess
import sys
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


def seed():
    from agent_journal.common import Api

    api = Api()
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
    print(FIXTURE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("AGENT_JOURNAL_DEV_HOME", ROOT / ".local/dev")),
    )
    parser.add_argument("command", choices=("serve", "seed", "token", "client"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "AGENT_JOURNAL_TOKEN": token(args.state / "token"),
        "AGENT_JOURNAL_URL": f"http://127.0.0.1:{args.port}",
        "AGENT_JOURNAL_HOME": str(args.state.resolve()),
    }
    os.environ.update(env)
    if args.command == "seed":
        seed()
    elif args.command == "token":
        print(env["AGENT_JOURNAL_TOKEN"])
    else:
        cli = (
            ["serve", "--port", str(args.port)]
            if args.command == "serve"
            else args.args
        )
        executable = Path(sys.executable).with_name("agent-journal")
        return subprocess.call([executable, *cli])
    return 0


if __name__ == "__main__":
    sys.exit(main())
