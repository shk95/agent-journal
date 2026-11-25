import argparse
import getpass
import os
import sqlite3
import subprocess
import sys
from contextlib import suppress
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .common import (
    Api,
    ConnectionRequired,
    JournalHTTPError,
    local_repo,
    repo_key,
    state_dir,
)


def current(explicit):
    project = (
        repo_key("https://github.com/" + explicit) if explicit else local_repo(".")
    )
    if not project:
        if explicit:
            raise ValueError("Specify a GitHub project as OWNER/REPO")
        raise ValueError(
            "No GitHub project found in the current directory. Run inside a GitHub checkout."
        )
    return project


def connect(url=None):
    if url is None:
        try:
            url = input("Journal server URL: ").strip()
        except EOFError:
            raise ConnectionRequired("Enter a journal server URL with --url") from None
    if not url:
        raise ValueError("Enter a journal server URL")
    token = getpass.getpass("Journal access token: ").strip()
    if not token:
        raise ValueError("Enter the journal access token")
    api = Api(url, token)
    projects = api.request("projects")
    api.save()
    print("Connected this machine.")
    return api, projects


def client_connection():
    try:
        return Api(), None
    except ConnectionRequired:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise
        return connect()


def format_bytes(size):
    for scale, unit in ((10**12, "TB"), (10**9, "GB"), (10**6, "MB"), (10**3, "KB")):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size:,} B"


def show_stats(api):
    try:
        data = api.request("stats")
    except JournalHTTPError as error:
        if error.status in (400, 404):
            raise RuntimeError(
                "Storage statistics are unavailable on this Journal server."
            ) from None
        raise
    print(f"Database  {format_bytes(data['database_bytes'])}")
    print(f"WAL       {format_bytes(data['wal_bytes'])}")
    recent = data["recent"]
    print(
        f"Last {recent['days']} days  {recent['turn_count']:,} turns"
        f"  {format_bytes(recent['turn_bytes'])}"
    )
    print()
    if not data["projects"]:
        print("No registered projects.")
        return
    width = max(len("Project"), max(len(row["project"]) for row in data["projects"]))
    print(
        f"{'Project':<{width}}  {'Turns':>8}  {'Notes':>8}"
        f"  {'Record data':>11}  Latest turn (UTC)"
    )
    for row in data["projects"]:
        latest = "–"
        if row["latest_turn_started"] is not None:
            latest = datetime.fromtimestamp(
                row["latest_turn_started"], timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
        size = format_bytes(row["turn_bytes"] + row["note_bytes"])
        print(
            f"{row['project']:<{width}}  {row['turn_count']:>8,}  {row['note_count']:>8,}"
            f"  {size:>11}  {latest}"
        )


def main():
    p = argparse.ArgumentParser(
        prog="agent-journal",
        description="Open the terminal journal for the current GitHub checkout.",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {version('agent-journal')}"
    )
    commands = p.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--database", type=Path)
    serve.add_argument("--deployment-id-file", type=Path)
    serve.add_argument("--maintenance-file", type=Path)
    snapshot = commands.add_parser("backup")
    snapshot.add_argument("--database", type=Path)
    snapshot.add_argument("--output", type=Path, required=True)
    commands.add_parser("disable").add_argument("--project", metavar="OWNER/REPO")
    tui = commands.add_parser("tui", help="open the terminal journal")
    tui.add_argument("--project", metavar="OWNER/REPO")
    tui.add_argument("--no-mouse", action="store_true", help="disable mouse input")
    web = commands.add_parser("web", help="open the web journal in a browser")
    web.add_argument("--project", metavar="OWNER/REPO")
    commands.add_parser("projects")
    commands.add_parser(
        "stats", help="show storage statistics for the connected Journal server"
    )
    commands.add_parser("server-version", help="show the connected Journal server version")
    commands.add_parser("setup-codex", help="install global Codex tools and recording hooks")
    commands.add_parser("codex-hook")
    connection = commands.add_parser("connect", help="set up the journal connection")
    connection.add_argument("--url")
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--project", metavar="OWNER/REPO")
    mcp.add_argument("--socket", type=Path)
    watch = commands.add_parser("observe")
    watch.add_argument("--socket", type=Path)
    watch.add_argument("--interval", type=float, default=3)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--codex-lock", type=int, help=argparse.SUPPRESS)
    args = p.parse_args(sys.argv[1:] or ["tui"])
    try:
        if args.command == "backup":
            from .backup import backup

            backup(args.database or state_dir() / "server.sqlite3", args.output)
            print("Journal backup created.")
            return
        if args.command == "connect":
            connect(args.url)
            return
        if args.command == "setup-codex":
            from .codex_integration import setup_codex

            setup_codex()
            return
        if args.command == "codex-hook":
            from .codex_integration import codex_hook

            codex_hook()
            return
        if args.command == "serve":
            from .client_state import write_state
            from .server import make_server

            status_path = os.environ.get("AGENT_JOURNAL_DEPLOYMENT_STATE")
            status = Path(status_path) if status_path else None
            if status:
                status.unlink(missing_ok=True)
            try:
                server = make_server(
                    args.host,
                    args.port,
                    args.database or state_dir() / "server.sqlite3",
                    os.environ.get("AGENT_JOURNAL_TOKEN", ""),
                    deployment_id=(
                        args.deployment_id_file.read_text(encoding="utf-8").strip()
                        if args.deployment_id_file
                        else None
                    ),
                    maintenance_file=args.maintenance_file,
                    deployment_probe_key=os.environ.get("AGENT_JOURNAL_DEPLOYMENT_KEY"),
                )
            except (OSError, ValueError, sqlite3.Error) as error:
                if status:
                    sqlite_code = getattr(error, "sqlite_errorcode", 0) & 255
                    transient = isinstance(error, OSError) or sqlite_code in {
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_NOMEM,
                        sqlite3.SQLITE_READONLY, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL,
                        sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM,
                    }
                    with suppress(OSError):
                        write_state(status, {
                            "phase": "migration" if isinstance(error, sqlite3.Error) else "startup",
                            "reason": str(error), "transient": transient,
                        })
                raise
            with server:
                print(f"Journal listening on {server.server_address}", flush=True)
                server.serve_forever()
            return
        if args.command == "tui":
            from .client_state import ClientState
            from .tui import run

            project = current(args.project)
            api, projects = client_connection()
            if projects is None and not ClientState(api, project).has_cache():
                projects = api.request("projects")
            if projects is not None and not any(row["project"] == project for row in projects):
                raise ValueError(
                    f"{project} is not registered. Ask Codex to register it with Agent Journal."
                )
            run(api, project, mouse=not args.no_mouse)
            return
        if args.command == "mcp":
            from .mcp_server import make_mcp

            make_mcp(
                default_project=current(args.project) if args.project else None,
                socket=args.socket,
            ).run()
            return
        api, projects = (
            (Api(), None) if args.command == "observe" else client_connection()
        )
        if args.command == "web":
            import webbrowser
            from urllib.parse import quote

            url = api.base + "/"
            if args.project:
                url += "#" + quote(current(args.project), safe="")
            print(url, flush=True)
            webbrowser.open_new_tab(url)
            return
        if args.command == "server-version":
            print(api.server_version() or "unknown")
        elif args.command == "stats":
            show_stats(api)
        elif args.command == "projects":
            if projects is None:
                projects = api.request("projects")
            for row in projects:
                print(row["project"], "active" if row["active"] else "disabled")
        elif args.command == "observe":
            from .observer import collect

            if args.interval <= 0:
                raise ValueError("Interval must be positive")
            collect(api, args.socket, args.interval, args.once, args.codex_lock)
        else:
            key = current(args.project)
            api.request("disable", {"project": key})
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
