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
import tempfile
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from agent_journal.common import Api
from tests.smoke_local import child
from tests.test_local_stack import CodexFixture, turn

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
        line = process.stdout.readline()
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
            fixture.server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude", help="candidate tag to skip during a release")
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
    with tempfile.TemporaryDirectory(prefix="aj-compat-", dir="/tmp") as temporary:
        root = Path(temporary)
        for tag in tags:
            folder = root / tag
            previous = install_tag(tag, folder)
            exchange(folder / "new-client", candidate, previous, server_tag=tag)
            exchange(folder / "old-client", previous, candidate)
            print(f"Client/server compatibility verified in both directions with {tag}.")


if __name__ == "__main__":
    main()
