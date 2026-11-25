"""Exercise both client/server directions against stable tags in the same major."""

import argparse
import ast
import asyncio
import io
import os
import re
import subprocess
import sys
import tarfile
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from agent_journal.common import Api
from tests.processes import child, cleanup_on_termination, startup_line
from tests.test_local_stack import CodexFixture, turn
from tests.workspace import test_workspace

PROJECT = "example/journal-compatibility"
THREAD = "fixture-session"
TURN = "fixture-turn"
PROMPT = "Verify a sample result."
NOTE = "Keep the sample result."
STABLE_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def install_tag(tag, folder):
    source = folder / "source"
    source.mkdir(parents=True)
    archive = subprocess.check_output(["git", "archive", tag])
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(source, filter="data")
    environment = folder / "venv"
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(environment)
    subprocess.run(
        ["uv", "sync", "--quiet", "--locked", "--project", source],
        env=env,
        check=True,
    )
    return environment / "bin" / "agent-journal"


async def read_and_write(client, env, cwd):
    params = StdioServerParameters(
        command=str(client), args=["mcp"], cwd=cwd, env=env
    )
    async with Client(params) as session:
        await session.call_tool(
            "add_note",
            {
                "project": PROJECT,
                "thread": THREAD,
                "turn": TURN,
                "request_id": "fixture-note",
                "body": NOTE,
            },
        )
        result = await session.call_tool(
            "read_journal", {"project": PROJECT, "axes": ["journey", "turn_notes"]}
        )
    data = result.structured_content
    if (
        [row["turn"] for row in data["journey"]] != [TURN]
        or [note["body"] for note in data["turn_notes"]] != [NOTE]
    ):
        raise RuntimeError("The client did not read the synced turn and note")


def exchange(folder, client, server, server_tag=None):
    folder.mkdir(parents=True)
    env = os.environ | {
        "AGENT_JOURNAL_HOME": str(folder / "journal"),
        "AGENT_JOURNAL_TOKEN": "compatibility-fixture",
    }
    command = [server, "serve", "--port", "0"]
    if server_tag:
        deployment = folder / "deployment-id"
        commit = git("rev-list", "-n", "1", server_tag)
        deployment.write_text(f"{server_tag}-{commit[:12]}\n", encoding="utf-8")
        command += ["--deployment-id-file", deployment]
    with child(command, env=env, stdout=subprocess.PIPE, text=True) as process:
        line = startup_line(process)
        prefix = "Journal listening on "
        if not line.startswith(prefix):
            raise RuntimeError("The compatibility server did not start")
        _, port = ast.literal_eval(line.removeprefix(prefix).strip())
        env["AGENT_JOURNAL_URL"] = f"http://127.0.0.1:{port}"
        with patch.dict(os.environ, {"AGENT_JOURNAL_HOME": env["AGENT_JOURNAL_HOME"]}):
            api = Api(env["AGENT_JOURNAL_URL"], env["AGENT_JOURNAL_TOKEN"])
        api.request("register", {"project": PROJECT})
        listed = subprocess.check_output([client, "projects"], env=env, text=True)
        if listed.strip() != f"{PROJECT} active":
            raise RuntimeError("The client did not list the registered project")
        if server_tag:
            reported = subprocess.check_output(
                [client, "server-version"], env=env, text=True
            ).strip()
            if reported != server_tag.removeprefix("v"):
                raise RuntimeError(f"The client reported {reported!r} for {server_tag}")

        started = int(api.request("projects")[0]["since"]) + 1
        socket = folder / "codex.sock"
        fixture = CodexFixture(
            socket,
            {
                "id": THREAD,
                "updatedAt": started + 1,
                "status": {"type": "idle"},
                "gitInfo": {"originUrl": f"https://github.com/{PROJECT}.git"},
            },
            [turn(TURN, started, PROMPT, "completed", "Sample verified.")],
        )
        try:
            subprocess.run(
                [client, "observe", "--once", "--socket", socket], env=env, check=True
            )
            transcript = api.request("transcript", project=PROJECT, thread=THREAD, turn=TURN)
            if transcript[0]["content"][0]["text"] != PROMPT:
                raise RuntimeError("The server did not store the client's transcript")
            asyncio.run(read_and_write(client, env, folder))
        finally:
            fixture.close()


def migrate_snapshot(folder, previous, candidate):
    state = folder / "new-client" / "journal"
    snapshot = folder / "database-copy.sqlite3"
    env = os.environ | {
        "AGENT_JOURNAL_HOME": str(state), "AGENT_JOURNAL_TOKEN": "migration-fixture",
    }
    subprocess.run(
        [previous, "backup", "--database", state / "server.sqlite3", "--output", snapshot],
        env=env, check=True, stdout=subprocess.DEVNULL,
    )
    with child([candidate, "serve", "--port", "0", "--database", snapshot],
               env=env, stdout=subprocess.PIPE, text=True) as process:
        prefix = "Journal listening on "
        line = startup_line(process)
        if not line.startswith(prefix):
            raise RuntimeError("The candidate did not start with the legacy snapshot")
        _, port = ast.literal_eval(line.removeprefix(prefix).strip())
        with patch.dict(os.environ, {"AGENT_JOURNAL_HOME": str(state)}):
            api = Api(f"http://127.0.0.1:{port}", env["AGENT_JOURNAL_TOKEN"])
        board = api.request("board", project=PROJECT)
        if [turn["turn"] for turn in board["turns"]] != [TURN]:
            raise RuntimeError("Journey was not preserved in the migrated snapshot")
        note = api.request("note", project=PROJECT, id="fixture-note")
        if (note["title"], note["body"], note["thread"], note["turn"]) != ("Untitled note", NOTE, THREAD, TURN):
            raise RuntimeError("The legacy note was not preserved in the migrated snapshot")
        if note["priority"] != "normal":
            raise RuntimeError("A migrated note should start with Normal status")
        stats = api.request("stats", project=PROJECT)
        if stats["projects"][0]["note_count"] != 1:
            raise RuntimeError("Storage statistics missed the migrated note")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude", help="candidate tag to skip during a release")
    parser.add_argument(
        "--database-migrations", action="store_true",
        help="also boot the candidate with a snapshot from each earlier server",
    )
    args = parser.parse_args()
    major = version("agent-journal").split(".")[0]
    tags = [
        tag for tag in git("tag", "--sort=version:refname", "--list", f"v{major}.*").splitlines()
        if STABLE_TAG.fullmatch(tag) and tag != args.exclude
    ]
    if not tags:
        print(f"No earlier stable tags in major {major}.")
        return
    candidate = Path(sys.executable).with_name("agent-journal")
    with cleanup_on_termination(), test_workspace("aj-compat-") as root:
        for tag in tags:
            folder = root / tag
            previous = install_tag(tag, folder)
            exchange(folder / "new-client", candidate, previous, server_tag=tag)
            exchange(folder / "old-client", previous, candidate)
            print(f"Client/server compatibility verified in both directions with {tag}.")
            if args.database_migrations:
                migrate_snapshot(folder, previous, candidate)
                print(f"Database snapshot migration verified with {tag}.")


if __name__ == "__main__":
    main()
