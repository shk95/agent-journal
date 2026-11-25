import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

PROJECT = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9_.-]+", re.IGNORECASE)
OBSERVATION_LEASE = 45
OBSERVATION_REFRESH = 15


def turn_start(turn, started):
    """Use the ID timestamp only when it agrees with the Codex start second."""
    try:
        identifier = UUID(turn)
    except (ValueError, TypeError, AttributeError):
        return started
    millis = identifier.int >> 80
    if identifier.version == 7 and millis // 1000 == started:
        return millis / 1000
    return started


def state_dir():
    folder = Path(os.environ.get("AGENT_JOURNAL_HOME", "~/.agent-journal")).expanduser()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def repo_key(remote):
    if not remote:
        return None
    if remote.startswith("git@github.com:"):
        path = remote.split(":", 1)[1]
    else:
        parts = urlsplit(remote)
        if parts.scheme not in ("ssh", "https") or parts.hostname != "github.com":
            return None
        path = parts.path.lstrip("/")
    key = path.rstrip("/").removesuffix(".git").lower()
    if PROJECT.fullmatch(key) and key.split("/")[1] not in (".", ".."):
        return key
    return None


def local_repo(cwd):
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
        return repo_key(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Api:
    def __init__(self):
        self.base = os.environ.get("AGENT_JOURNAL_URL", "http://127.0.0.1:8765").rstrip(
            "/"
        )
        self.token = os.environ.get("AGENT_JOURNAL_TOKEN", "")
        parts = urlsplit(self.base)
        if parts.scheme != "https" and not (
            parts.scheme == "http"
            and parts.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Use HTTPS for a remote journal server")
        if not self.token:
            raise ValueError("Set AGENT_JOURNAL_TOKEN first")

    @property
    def cache_key(self):
        return hashlib.sha256(self.base.encode()).hexdigest()[:16]

    def request(self, path, body=None, **query):
        url = self.base + "/api/" + path
        if query:
            url += "?" + urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = Request(
            url,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=8) as r:
                return json.load(r)
        except HTTPError as err:
            raise RuntimeError(
                f"Journal HTTP {err.code}: {err.read(400).decode(errors='replace')}"
            ) from err
