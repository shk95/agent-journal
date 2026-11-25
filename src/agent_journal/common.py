import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

PROJECT = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9_.-]+", re.IGNORECASE)
OBSERVATION_LEASE = 45
OBSERVATION_REFRESH = 15


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect())


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


class ConnectionRequired(ValueError):
    pass


class Api:
    def __init__(self, base=None, token=None):
        path = state_dir() / "connection.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = {}
        self.base = (
            base
            or os.environ.get("AGENT_JOURNAL_URL")
            or saved.get("url")
            or "http://127.0.0.1:8765"
        ).rstrip("/")
        self.token = (
            token or os.environ.get("AGENT_JOURNAL_TOKEN") or saved.get("token", "")
        )
        parts = urlsplit(self.base)
        if (
            not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "Enter a server URL without credentials, a query, or a fragment"
            )
        if parts.scheme != "https" and not (
            parts.scheme == "http"
            and parts.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Use HTTPS for a remote journal server")
        if not self.token:
            raise ConnectionRequired(
                "Run agent-journal connect to set up the journal connection on this machine"
            )

    def save(self):
        parent = state_dir()
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, delete=False
        ) as file:
            tmp = Path(file.name)
            try:
                json.dump({"url": self.base, "token": self.token}, file)
                file.close()
                tmp.replace(parent / "connection.json")
            finally:
                tmp.unlink(missing_ok=True)

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
            with HTTP.open(req, timeout=8) as r:
                return json.load(r)
        except HTTPError as err:
            raise RuntimeError(
                f"Journal HTTP {err.code}: {err.read(400).decode(errors='replace')}"
            ) from err
