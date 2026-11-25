"""Bootstrap Agent Journal on Linux and apply published server releases."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.request import Request, urlopen

REPO = "shk95/agent-journal"
API = f"https://api.github.com/repos/{REPO}"
ROOT = Path("/opt/agent-journal")
DATA = Path("/var/lib/agent-journal")
CONFIG = Path("/etc/agent-journal")
UNIT = "agent-journal.service"
TAG = re.compile(r"server-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[a-f0-9]{40}")
ID = re.compile(r"(?:server-v[0-9]+\.[0-9]+\.[0-9]+|bootstrap)-[a-f0-9]{12}")
DOMAIN = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE).stdout.strip()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def fetch(url, limit):
    with urlopen(
        Request(url, headers={"User-Agent": "agent-journal-updater"}), timeout=15
    ) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Release response is too large")
    return data


def github(path):
    return json.loads(fetch(API + path, 2_000_000))


def version(tag):
    match = TAG.fullmatch(tag or "")
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def select_release(releases, current):
    return max(
        (
            item
            for item in releases
            if TAG.fullmatch(item["tag_name"])
            and version(item["tag_name"]) > version(current)
            and not item["draft"]
            and not item["prerelease"]
        ),
        key=lambda item: version(item["tag_name"]),
        default=None,
    )


def release_commit(release):
    tag = release["tag_name"]
    if not TAG.fullmatch(tag):
        raise ValueError("Invalid server release tag")
    asset = next(
        (item for item in release["assets"] if item["name"] == "server-release.json"),
        None,
    )
    if not asset or not asset.get("digest"):
        raise ValueError("Server release manifest or its SHA-256 digest is missing")
    raw = fetch(
        f"https://github.com/{REPO}/releases/download/{tag}/server-release.json", 4096
    )
    if asset["digest"] != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError("Server release manifest failed its digest check")
    manifest = json.loads(raw)
    commit = manifest.get("commit", "")
    if manifest.get("tag") != tag or not SHA.fullmatch(commit):
        raise ValueError("Invalid server release manifest")
    target = github("/git/ref/tags/" + tag)["object"]
    if target["type"] == "tag" and SHA.fullmatch(target["sha"]):
        target = github("/git/tags/" + target["sha"])["object"]
    if target["type"] != "commit" or target["sha"] != commit:
        raise ValueError("Server release commit does not match the Git tag")
    return commit


class Deployment:
    def __init__(self, root=ROOT, data=DATA):
        self.root, self.data = root, data
        self.current = root / "current"
        self.pending = root / "pending.json"
        self.maintenance = root / "maintenance"
        self.blocked = root / "failed.json"

    @contextlib.contextmanager
    def lock(self):
        with (self.root / "update.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def slot(self, identifier):
        if not ID.fullmatch(identifier):
            raise ValueError("Invalid deployment identifier")
        return self.root / "releases" / identifier

    def active(self):
        if not self.current.is_symlink():
            return None
        slot = self.slot(self.current.readlink().name)
        return read_json(slot / "release.json")

    def prepare(self, commit, tag=None, checkout=None):
        if not SHA.fullmatch(commit) or (tag and not TAG.fullmatch(tag)):
            raise ValueError("Invalid source revision")
        baseline = tag
        if checkout:
            with (checkout / "pyproject.toml").open("rb") as source:
                baseline = "server-v" + tomllib.load(source)["project"]["version"]
            if not TAG.fullmatch(baseline):
                raise ValueError("The server package must have a three-part version")
        identifier = f"{tag or 'bootstrap'}-{commit[:12]}"
        slot = self.slot(identifier)
        slot.mkdir(parents=True, exist_ok=True)
        slot.parent.chmod(0o755)
        slot.chmod(0o755)
        link = slot / "package"
        if not (link / "bin" / "agent-journal").is_file():
            source = str(checkout) if checkout else f"github:{REPO}/{commit}"
            nix = read_json(self.root / "host.json")["nix"]
            run(
                nix,
                "build",
                "--extra-experimental-features",
                "nix-command flakes",
                "--no-write-lock-file",
                "--out-link",
                str(link),
                source + "#server",
            )
        db_dir = self.data / identifier
        self.create_data_dir(db_dir)
        if not (slot / "data").is_symlink():
            (slot / "data").symlink_to(db_dir)
        (slot / "id").write_text(identifier + "\n", encoding="utf-8")
        (slot / "id").chmod(0o644)
        atomic_json(
            slot / "release.json", {"id": identifier, "tag": baseline, "commit": commit}
        )
        return identifier

    def create_data_dir(self, path):
        path.mkdir(exist_ok=True, mode=0o700)
        journal = pwd.getpwnam("journal")
        os.chown(path, journal.pw_uid, journal.pw_gid)

    def service(self, operation):
        run("systemctl", operation, UNIT)

    def point_at(self, identifier):
        if identifier is None:
            self.current.unlink(missing_ok=True)
            return
        temp = self.root / "current.new"
        temp.unlink(missing_ok=True)
        temp.symlink_to(self.slot(identifier))
        temp.replace(self.current)

    def wait_ready(self, identifier):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:8765/healthz", timeout=2) as response:
                    status = json.load(response)
                if status == {"status": "ready", "deployment": identifier}:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Deployment {identifier} did not become ready")

    def snapshot(self, previous, candidate):
        source = self.slot(previous)
        target = self.slot(candidate)
        run(
            "runuser",
            "-u",
            "journal",
            "--",
            str(source / "package/bin/agent-journal"),
            "backup",
            "--database",
            str(source / "data/journal.sqlite3"),
            "--output",
            str(target / "data/journal.sqlite3"),
        )

    def recover(self):
        if self.pending.exists():
            state = read_json(self.pending)
            previous, candidate = state["previous"], state["candidate"]
            self.maintenance.touch()
            self.service("stop")
            self.point_at(previous)
            if previous:
                self.service("start")
                self.wait_ready(previous)
            failed = read_json(self.blocked) if self.blocked.exists() else []
            atomic_json(self.blocked, sorted(set(failed) | {candidate}))
            self.pending.unlink()
        if self.maintenance.exists():
            current = self.active()
            if current:
                self.wait_ready(current["id"])
            self.maintenance.unlink()

    def apply(self, identifier):
        active = self.active()
        previous = active["id"] if active else None
        if (self.slot(identifier) / "data/journal.sqlite3").exists():
            raise RuntimeError("Candidate database already exists. Use a new release.")
        atomic_json(self.pending, {"previous": previous, "candidate": identifier})
        self.maintenance.touch()
        try:
            if previous:
                self.service("stop")
                self.snapshot(previous, identifier)
            self.point_at(identifier)
            self.service("start")
            self.wait_ready(identifier)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            self.recover()
            raise
        self.pending.unlink()
        self.maintenance.unlink()
        print(f"Agent Journal is serving {identifier}.", flush=True)

    def update(self):
        self.recover()
        active = self.active()
        if not active:
            raise RuntimeError("Install Agent Journal before checking for updates")
        releases = []
        for page in range(1, 11):
            batch = github(f"/releases?per_page=100&page={page}")
            releases.extend(batch)
            if len(batch) < 100:
                break
        release = select_release(releases, active["tag"])
        if release is None:
            print("No newer server release.")
            return
        tag = release["tag_name"]
        commit = release_commit(release)
        identifier = f"{tag}-{commit[:12]}"
        if self.blocked.exists() and identifier in read_json(self.blocked):
            print(f"Skipping failed deployment {identifier}.")
            return
        self.apply(self.prepare(commit, tag))


def find_nix():
    for path in (
        shutil.which("nix"),
        "/nix/var/nix/profiles/default/bin/nix",
        "/run/current-system/sw/bin/nix",
    ):
        if path and Path(path).is_file():
            return path
    raise RuntimeError("Install Nix before deploying Agent Journal")


def install(deployment, domain):
    checkout = Path(__file__).resolve().parents[1]
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("A running systemd instance is required")
    if domain and not DOMAIN.fullmatch(domain):
        raise ValueError("Use a DNS hostname for --domain")
    if deployment.current.is_symlink() or (Path("/etc/systemd/system") / UNIT).exists():
        raise RuntimeError(
            "A system service already exists. Use docs/OPERATIONS.md to migrate it."
        )
    if output("git", "-C", str(checkout), "status", "--porcelain"):
        raise RuntimeError("Install from a clean checkout of a reviewed revision")
    commit = output("git", "-C", str(checkout), "rev-parse", "HEAD")
    nix = find_nix()
    proxy = shutil.which("caddy") if domain else None
    if domain and not proxy:
        raise RuntimeError("Install Caddy before configuring public HTTPS")
    if proxy:
        for port in (80, 443):
            with socket.socket() as listener:
                try:
                    listener.bind(("0.0.0.0", port))
                except OSError as error:
                    raise RuntimeError(
                        "Ports 80 and 443 must be free for the managed proxy"
                    ) from error
    try:
        pwd.getpwnam("journal")
    except KeyError:
        run(
            "useradd",
            "--system",
            "--user-group",
            "--shell",
            "/usr/sbin/nologin",
            "journal",
        )
    deployment.root.mkdir(parents=True, exist_ok=True, mode=0o755)
    deployment.data.mkdir(parents=True, exist_ok=True, mode=0o711)
    CONFIG.mkdir(exist_ok=True, mode=0o700)
    token_path = CONFIG / "server.env"
    if not token_path.exists():
        with token_path.open("x", encoding="utf-8") as handle:
            handle.write(f"AGENT_JOURNAL_TOKEN={secrets.token_urlsafe(32)}\n")
    atomic_json(deployment.root / "host.json", {"nix": nix})
    shutil.copyfile(__file__, deployment.root / "server_deploy.py")
    # systemd and the service user need to traverse this directory.
    deployment.root.chmod(0o755)
    deployment.data.chmod(0o711)
    units = Path("/etc/systemd/system")
    with deployment.lock():
        identifier = deployment.prepare(commit, checkout=checkout)
        for name in (
            UNIT,
            "agent-journal-update.service",
            "agent-journal-update.timer",
        ):
            shutil.copyfile(checkout / "deploy" / name, units / name)
            (units / name).chmod(0o644)
        run("systemctl", "daemon-reload")
        deployment.apply(identifier)
        run("systemctl", "enable", UNIT)
        run("systemctl", "enable", "--now", "agent-journal-update.timer")
    if proxy:
        install_proxy(proxy, domain, checkout, units)
    print(f"Access token is in {token_path}. Keep it private.")


def install_proxy(binary, domain, checkout, units):
    proxy_dir = Path("/etc/agent-journal-proxy")
    proxy_dir.mkdir(exist_ok=True, mode=0o755)
    proxy_dir.chmod(0o755)
    config = proxy_dir / "Caddyfile"
    config.write_text(
        f"{domain} {{\n    reverse_proxy 127.0.0.1:8765\n}}\n", encoding="utf-8"
    )
    config.chmod(0o644)
    name = "agent-journal-proxy.service"
    template = (checkout / "deploy" / name).read_text(encoding="utf-8")
    (units / name).write_text(template.replace("@CADDY@", binary), encoding="utf-8")
    (units / name).chmod(0o644)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", name)
    print(f"HTTPS proxy is configured for {domain}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install").add_argument("--domain")
    commands.add_parser("update")
    commands.add_parser("recover")
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux":
        parser.error("Run as root on a Linux VM with systemd")
    os.umask(0o077)
    deployment = Deployment()
    try:
        if args.command == "install":
            install(deployment, args.domain)
        else:
            with deployment.lock():
                getattr(deployment, args.command)()
    except (
        OSError,
        KeyError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
