import argparse
import getpass
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from .common import Api, local_repo, repo_key, state_dir


def current(explicit):
    project = (
        repo_key("https://github.com/" + explicit) if explicit else local_repo(".")
    )
    if not project:
        raise ValueError("Specify --project OWNER/REPO or run inside a GitHub checkout")
    return project


def verified(project):
    result = subprocess.run(
        ["gh", "api", "repos/" + project, "--jq", ".full_name"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode or result.stdout.strip().lower() != project:
        raise ValueError(
            f"GitHub repository verification failed for {project}: {result.stderr.strip()}"
        )
    return project


def main():
    p = argparse.ArgumentParser(prog="agent-journal")
    commands = p.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--database", type=Path)
    snapshot = commands.add_parser("backup")
    snapshot.add_argument("--database", type=Path)
    snapshot.add_argument("--output", type=Path, required=True)
    for name in ("register", "disable", "tui"):
        commands.add_parser(name).add_argument("--project", metavar="OWNER/REPO")
    commands.add_parser("projects")
    connect = commands.add_parser("connect")
    connect.add_argument("--url", required=True)
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--project", metavar="OWNER/REPO")
    mcp.add_argument("--socket", type=Path)
    watch = commands.add_parser("observe")
    watch.add_argument("--socket", type=Path)
    watch.add_argument("--interval", type=float, default=3)
    watch.add_argument("--once", action="store_true")
    args = p.parse_args()
    try:
        if args.command == "backup":
            from .backup import backup

            backup(args.database or state_dir() / "server.sqlite3", args.output)
            print("Journal backup created.")
            return
        if args.command == "connect":
            token = getpass.getpass("Journal access token: ").strip()
            if not token:
                raise ValueError("Enter the journal access token")
            api = Api(args.url, token)
            api.request("projects")
            api.save()
            print("Connected this machine.")
            return
        if args.command == "serve":
            from .server import make_server

            with make_server(
                args.host,
                args.port,
                args.database or state_dir() / "server.sqlite3",
                os.environ.get("AGENT_JOURNAL_TOKEN", ""),
            ) as server:
                print(f"Journal listening on {server.server_address}", flush=True)
                server.serve_forever()
            return
        api = Api()
        if args.command == "projects":
            for row in api.request("projects"):
                print(row["project"], "active" if row["active"] else "disabled")
        elif args.command == "mcp":
            from .mcp_server import make_mcp

            make_mcp(
                api, current(args.project) if args.project else None, args.socket
            ).run()
        elif args.command == "observe":
            from .observer import Cache, observe

            if args.interval <= 0:
                raise ValueError("Interval must be positive")
            import threading

            from .summary import worker

            path = state_dir() / f"observer-{api.cache_key}.sqlite3"
            cache = Cache(path)
            stop = threading.Event()
            if not args.once:
                threading.Thread(target=worker, args=(path, stop), daemon=True).start()
            try:
                observe(api, cache, args.socket, args.interval, args.once)
            finally:
                stop.set()
                cache.db.close()
        elif args.command == "tui":
            from .tui import run

            run(api, current(args.project))
        else:
            key = current(args.project)
            if args.command == "register":
                verified(key)
            api.request(args.command, {"project": key})
            print(key)
    except KeyboardInterrupt:
        return
    except (
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        sqlite3.Error,
    ) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
