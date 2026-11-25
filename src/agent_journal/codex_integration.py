"""Install Codex hooks and handle their local session notifications."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path

from .common import Api, ConnectionRequired, codex_home, state_dir

SESSION_GRACE = 30
FINAL_SYNC_GRACE = 6
FINAL_SYNC_DEADLINE = 100


def client_executable():
    executable = Path(sys.argv[0]).absolute()
    if executable.name == ".agent-journal-wrapped":
        executable = executable.with_name("agent-journal")
    if executable.name == "__main__.py":
        command = shutil.which("agent-journal")
        if not command:
            raise ValueError("Run Codex setup with the installed agent-journal executable")
        executable = Path(command).absolute()
    return str(executable)


def setup_codex():
    client = client_executable()
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "hooks.json"
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = None
    document = json.loads(original) if original is not None else {}
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise ValueError("Codex hooks.json must contain a hooks object")
    hooks = document.setdefault("hooks", {})
    command = shlex.join([client, "codex-hook"])

    for event in ("SessionStart", "SessionEnd"):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Codex {event} hooks must be a list")
        owned = {"type": "command", "command": command, "timeout": 3}
        updated = []
        installed = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid Codex {event} hook group")
            handlers = []
            for handler in group["hooks"]:
                if not is_journal_hook(handler, command):
                    handlers.append(handler)
                elif not installed:
                    handlers.append(owned)
                    installed = True
            updated.append(group | {"hooks": handlers})
        if not installed:
            updated.append({"hooks": [owned]})
        hooks[event] = updated

    entry = subprocess.run(
        ["codex", "mcp", "get", "journal", "--json"], capture_output=True, text=True
    )
    transport = json.loads(entry.stdout)["transport"] if not entry.returncode else {}
    if transport.get("command") != client or transport.get("args") != ["mcp"]:
        subprocess.run(["codex", "mcp", "add", "journal", "--", client, "mcp"], check=True)
    if original is None or document != json.loads(original):
        file = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=home, delete=False)
        temporary = Path(file.name)
        try:
            with file:
                json.dump(document, file, indent=2)
                file.write("\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print("Codex hooks are installed. Review the Agent Journal hooks in /hooks once.")
    print("Open a new Codex session after trusting the hooks to start automatic collection.")


def is_journal_hook(handler, command):
    value = handler.get("command") if isinstance(handler, dict) else None
    if not isinstance(value, str):
        return False
    if value == command:
        return True
    try:
        args = shlex.split(value)
    except ValueError:
        return False
    return args[1:] == ["codex-hook"] and Path(args[0]).name == "agent-journal"


def session_key(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()


class Lifecycle:
    def __init__(self, path):
        self.path = path
        self.path.mkdir(exist_ok=True, mode=0o700)
        self.idle_since = None
        self.codex_unavailable_since = None

    @contextmanager
    def events(self):
        with self.path.joinpath("events.lock").open("a") as lock:
            flock(lock, LOCK_EX)
            yield

    def prune(self, loaded):
        if loaded is None:
            now = time.monotonic()
            if self.codex_unavailable_since is None:
                self.codex_unavailable_since = now
            if now - self.codex_unavailable_since < SESSION_GRACE:
                return
            loaded = set()
        else:
            self.codex_unavailable_since = None
        loaded_keys = {session_key(session_id) for session_id in loaded}
        cutoff = time.time() - SESSION_GRACE
        with self.events():
            for marker in self.path.glob("session-*"):
                if (
                    marker.name.removeprefix("session-") not in loaded_keys
                    and marker.stat().st_mtime < cutoff
                ):
                    marker.unlink()

    def finished(self, cache):
        if next(self.path.glob("session-*"), None):
            self.idle_since = None
            return False
        now = time.monotonic()
        if self.idle_since is None:
            self.idle_since = now
        elapsed = now - self.idle_since
        if elapsed < FINAL_SYNC_GRACE:
            return False
        return elapsed >= FINAL_SYNC_DEADLINE or not cache.has_pending_work()


def collector_lock(path):
    lock = path.with_suffix(".lock").open("a")
    try:
        flock(lock, LOCK_EX | LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def observer_path(api):
    return state_dir() / f"observer-{api.cache_key}.sqlite3"


def codex_hook():
    event = json.load(sys.stdin)
    session_id = event.get("session_id")
    kind = event.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id or kind not in (
        "SessionStart", "SessionEnd"
    ):
        return
    state = state_dir().absolute()
    lifecycle = Lifecycle(state / "codex-sessions")
    marker = lifecycle.path / ("session-" + session_key(session_id))
    with lifecycle.events():
        if kind == "SessionEnd":
            marker.unlink(missing_ok=True)
            return
        try:
            api = Api()
        except ConnectionRequired:
            return
        marker.touch(mode=0o600)
        lock = collector_lock(observer_path(api))
        if lock is None:
            return
        with lock:
            env = os.environ | {
                "AGENT_JOURNAL_HOME": str(state),
                "CODEX_HOME": str(codex_home().absolute()),
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            }
            log_path = state / "observer.log"
            # Pass the locked descriptor so concurrent hooks see this worker immediately.
            with os.fdopen(
                os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a"
            ) as log:
                subprocess.Popen(
                    [
                        client_executable(),
                        "observe",
                        "--codex-lock",
                        str(lock.fileno()),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=state,
                    env=env,
                    start_new_session=True,
                    pass_fds=(lock.fileno(),),
                )
