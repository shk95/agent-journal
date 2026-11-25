"""Bootstrap Agent Journal on Linux and apply published server releases."""

import argparse
import contextlib
import errno
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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = "shk95/agent-journal"
API = f"https://api.github.com/repos/{REPO}"
ROOT = Path("/opt/agent-journal")
DATA = Path("/var/lib/agent-journal")
CONFIG = Path("/etc/agent-journal")
UNIT = "agent-journal.service"
RECOVERY_UNIT = "agent-journal-recover.service"
UPDATE_SERVICE = "agent-journal-update.service"
UPDATE_TIMER = "agent-journal-update.timer"
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[a-f0-9]{40}")
ID = re.compile(r"(?:(?:server-)?v[0-9]+\.[0-9]+\.[0-9]+|bootstrap)-[a-f0-9]{12}")
MANIFEST = "server-release.json"
DATABASE_FILES = ("journal.sqlite3", "journal.sqlite3-wal", "journal.sqlite3-shm",
                  "journal.sqlite3-journal", "startup-error.json")
DIAGNOSTIC_RETENTION = 7 * 86400
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
    sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove(path):
    path.unlink(missing_ok=True)
    sync_directory(path.parent)


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
    match = TAG.fullmatch((tag or "").removeprefix("server-"))
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def select_release(releases, current):
    def stable(item):
        if not isinstance(item, dict):
            return False
        tag = item.get("tag_name")
        assets = item.get("assets")
        return (
            isinstance(tag, str) and TAG.fullmatch(tag)
            and (current is None or version(tag) > version(current))
            and item.get("draft") is False and item.get("prerelease") is False
            and isinstance(assets, list)
            and any(isinstance(asset, dict) and asset.get("name") == MANIFEST for asset in assets)
        )

    return max(
        (item for item in releases if stable(item)),
        key=lambda item: version(item["tag_name"]),
        default=None,
    )


def published_releases():
    releases = []
    for page in range(1, 11):
        batch = github(f"/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Invalid GitHub release listing")
        releases.extend(batch)
        if len(batch) < 100:
            break
    return releases


def latest_release(current=None):
    return select_release(published_releases(), current)


def install_release(tag):
    if tag is None:
        release = latest_release()
    else:
        if not TAG.fullmatch(tag):
            raise ValueError("Use a stable release tag in the form vX.Y.Z")
        release = select_release([github("/releases/tags/" + tag)], None)
    if release is None:
        raise ValueError("No published stable server release found")
    return release["tag_name"], release_commit(release)


def release_manifest(release):
    tag = release["tag_name"]
    if not TAG.fullmatch(tag):
        raise ValueError("Invalid server release tag")
    asset = next(
        (item for item in release["assets"]
         if isinstance(item, dict) and item.get("name") == MANIFEST),
        None,
    )
    if not asset or not asset.get("digest"):
        raise ValueError("Server release manifest or its SHA-256 digest is missing")
    raw = fetch(f"https://github.com/{REPO}/releases/download/{tag}/{MANIFEST}", 4096)
    if asset["digest"] != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError("Server release manifest failed its digest check")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid server release manifest")
    commit = manifest.get("commit", "")
    if manifest.get("tag") != tag or not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("Invalid server release manifest")
    target = github("/git/ref/tags/" + tag)["object"]
    if target["type"] == "tag" and SHA.fullmatch(target["sha"]):
        target = github("/git/tags/" + target["sha"])["object"]
    if target["type"] != "commit" or target["sha"] != commit:
        raise ValueError("Server release commit does not match the Git tag")
    sources = manifest.get("upgrade_from")
    if "upgrade_from" in manifest and (
        not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources)
        or not all(
            set(source) == {"minimum", "maximum"}
            and all(isinstance(tag, str) and TAG.fullmatch(tag) for tag in source.values())
            and version(source["minimum"]) <= version(source["maximum"]) <= version(tag)
            for source in sources
        )
    ):
        raise ValueError("Invalid server upgrade source ranges")
    return manifest


def release_commit(release):
    return release_manifest(release)["commit"]


def supports_upgrade(manifest, active_tag):
    return any(
        version(bounds["minimum"]) <= version(active_tag) <= version(bounds["maximum"])
        for bounds in manifest.get("upgrade_from", ())
    )


class ReadinessError(RuntimeError):
    def __init__(self, message, phase, *, transient=False):
        super().__init__(message)
        self.phase = phase
        self.transient = transient


def host_failure(error):
    return isinstance(error, OSError) and error.errno in {
        errno.EIO, errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.ENOMEM,
        errno.EMFILE, errno.ENFILE, errno.EROFS,
    }


def host_setting(path, name):
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name and value.strip():
            return value.strip().strip('"\'')
    raise ValueError(f"Missing {name} in {path.name}")


class Deployment:
    def __init__(self, root=ROOT, data=DATA, config=CONFIG):
        self.root, self.data, self.config = root, data, config
        self.current = root / "current"
        self.pending = root / "pending.json"
        self.maintenance = root / "maintenance"
        self.blocked = root / "failed.json"
        self.commits = root / "commits"
        self.backoff = root / "backoff.json"
        self.cleanup_error = root / "cleanup-error.json"

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
                baseline = "v" + tomllib.load(source)["project"]["version"]
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
        identity = slot / "id.tmp"
        with identity.open("w", encoding="utf-8") as handle:
            handle.write(identifier + "\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        identity.replace(slot / "id")
        atomic_json(
            slot / "release.json", {"id": identifier, "tag": baseline, "commit": commit}
        )
        sync_directory(slot.parent)
        sync_directory(self.root)
        sync_directory(self.data)
        return identifier

    def create_data_dir(self, path):
        path.mkdir(exist_ok=True, mode=0o700)
        journal = pwd.getpwnam("journal")
        os.chown(path, journal.pw_uid, journal.pw_gid)

    def service(self, operation):
        if operation == "start":
            # An explicit rollback must be able to start after candidate crashes reach StartLimit.
            subprocess.run(
                ["systemctl", "reset-failed", UNIT], check=False, capture_output=True,
            )
        run("systemctl", operation, UNIT)

    def point_at(self, identifier):
        if identifier is None:
            remove(self.current)
            return
        temp = self.root / "current.new"
        temp.unlink(missing_ok=True)
        temp.symlink_to(self.slot(identifier))
        temp.replace(self.current)
        sync_directory(self.root)

    def wait_ready(self, identifier, *, timeout=30):
        deadline = time.monotonic() + timeout
        url = "http://127.0.0.1:8765"
        while time.monotonic() < deadline:
            try:
                with urlopen(url + "/healthz", timeout=2) as response:
                    status = json.load(response)
                if status == {"status": "ready", "deployment": identifier}:
                    return
            except (OSError, ValueError):
                pass
            marker = self.database(identifier).parent / "startup-error.json"
            if marker.is_file():
                failure = read_json(marker)
                raise ReadinessError(failure["reason"], failure["phase"],
                                     transient=failure["transient"])
            time.sleep(0.5)
        raise ReadinessError(
            f"Deployment {identifier} did not open its database and report ready. "
            f"Inspect journalctl -u {UNIT}.",
            "startup",
        )

    def check_application(self, identifier, legacy=False):
        token = host_setting(self.config / "server.env", "AGENT_JOURNAL_TOKEN")
        key = host_setting(self.config / "deployment-probe.env", "AGENT_JOURNAL_DEPLOYMENT_KEY")
        req = Request(
            "http://127.0.0.1:8765/internal/deployment",
            headers={"Authorization": "Bearer " + token, "X-Journal-Deployment-Key": key},
        )
        try:
            with urlopen(req, timeout=8) as response:
                status = json.load(response)
        except HTTPError as error:
            if legacy and error.code in (404, 503) and not error.headers.get("X-Journal-Deployment-Probe"):
                # An explicitly installed legacy baseline has no predecessor to retire.
                return
            raise ReadinessError(
                f"Authenticated application read failed for {identifier} (HTTP {error.code}).",
                "readiness",
            ) from error
        except (OSError, ValueError) as error:
            raise ReadinessError(
                f"Authenticated application read failed for {identifier}.", "readiness"
            ) from error
        if status != {"status": "ready", "deployment": identifier}:
            raise ReadinessError(f"Unexpected authenticated read result for {identifier}.", "readiness")

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
        self.sync_database(candidate)

    def sync_database(self, identifier):
        path = self.database(identifier)
        with path.open("rb") as database:
            os.fsync(database.fileno())
        sync_directory(path.parent)

    def database(self, identifier):
        self.slot(identifier)
        directory = self.data / identifier
        if directory.is_symlink():
            raise ValueError("Invalid deployment data directory")
        return directory / "journal.sqlite3"

    def failures(self):
        data = read_json(self.blocked) if self.blocked.exists() else {}
        if isinstance(data, list):
            return {
                identifier: {
                    "release": identifier, "phase": "unknown",
                    "reason": "Blocked by the previous updater",
                    "timestamp": self.blocked.stat().st_mtime,
                    "blocked": True,
                }
                for identifier in data
            }
        return data

    def record_failure(self, identifier, failure, blocked):
        failures = self.failures()
        failures[identifier] = {"release": identifier, **failure, "blocked": blocked}
        atomic_json(self.blocked, failures)

    def committed(self, state):
        switch = state.get("switch", "")
        if not re.fullmatch(r"[a-f0-9]{32}", switch):
            return False
        path = self.commits / (switch + ".json")
        if not path.is_file():
            return False
        record = read_json(path)
        if any(record.get(key) != state[key] for key in ("switch", "previous", "candidate")):
            raise RuntimeError("Commit record does not match the pending deployment.")
        return True

    def fence(self):
        self.maintenance.touch(exist_ok=True)
        sync_directory(self.root)

    def reconcile(self, *, boot=False):
        if not self.pending.exists() and not self.maintenance.exists():
            return
        self.fence()
        if self.pending.exists():
            state = read_json(self.pending)
            committed = self.committed(state)
            selected = state["candidate"] if committed else state["previous"]
            if not boot:
                self.service("stop")
            if selected and not self.database(selected).is_file():
                raise RuntimeError(f"Recovery database is missing for {selected}.")
            self.point_at(selected)
            if selected and not boot:
                self.service("start")
                self.wait_ready(selected)
            if not committed:
                failure = state.get("failure") or {
                    "phase": state.get("phase", "switch"),
                    "reason": "Deployment was interrupted",
                    "timestamp": time.time(),
                    "confirmed": False,
                }
                self.record_failure(state["candidate"], failure, failure["confirmed"])
            remove(self.pending)
        elif self.active() and not boot:
            self.wait_ready(self.active()["id"])
        elif self.active() and not self.database(self.active()["id"]).is_file():
            raise RuntimeError("The active deployment database is missing.")
        remove(self.maintenance)

    def recover(self):
        self.reconcile()

    def boot_recover(self):
        self.reconcile(boot=True)

    def set_phase(self, state, phase):
        state["phase"] = phase
        atomic_json(self.pending, state)

    def apply(self, identifier, *, legacy=False):
        active = self.active()
        previous = active["id"] if active else None
        if self.database(identifier).exists():
            raise RuntimeError("Candidate database already exists. Use a new release.")
        if self.pending.exists() or self.maintenance.exists():
            raise RuntimeError("Reconcile the pending deployment before starting another.")
        state = {
            "previous": previous, "candidate": identifier, "switch": secrets.token_hex(16),
            "phase": "fence",
        }
        atomic_json(self.pending, state)
        self.fence()
        try:
            if previous:
                self.set_phase(state, "snapshot")
                self.service("stop")
                self.snapshot(previous, identifier)
            self.set_phase(state, "startup")
            self.point_at(identifier)
            self.service("start")
            self.wait_ready(identifier)
            self.set_phase(state, "readiness")
            self.check_application(identifier, legacy=legacy)
            self.set_phase(state, "commit")
            self.sync_database(identifier)
            self.commits.mkdir(exist_ok=True, mode=0o700)
            sync_directory(self.root)
            atomic_json(
                self.commits / (state["switch"] + ".json"),
                state | {"timestamp": time.time(), "previous_cleaned": previous is None},
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            phase = getattr(error, "phase", state["phase"])
            state["failure"] = {
                "phase": phase, "reason": str(error), "timestamp": time.time(),
                "confirmed": phase in ("migration", "startup", "readiness")
                and not (host_failure(error) or getattr(error, "transient", False)),
            }
            atomic_json(self.pending, state)
            self.recover()
            raise
        remove(self.pending)
        remove(self.maintenance)
        print(f"Agent Journal is serving {identifier}.", flush=True)
        self.try_cleanup()

    def protected(self):
        active = self.active()
        protected = {active["id"]} if active else set()
        if self.pending.exists():
            state = read_json(self.pending)
            protected.update((state["previous"], state["candidate"]))
        return protected

    def delete_database_files(self, directory):
        if directory.is_symlink():
            raise ValueError("Refusing to follow a deployment data symlink")
        if directory.exists():
            for name in DATABASE_FILES:
                remove(directory / name)

    def cleanup(self):
        protected = self.protected()
        now = time.time()
        try:
            for path in self.commits.glob("*.json"):
                record = read_json(path)
                identifier = record["previous"]
                if record["previous_cleaned"] or identifier in protected:
                    continue
                self.delete_database_files(self.database(identifier).parent)
                atomic_json(path, record | {"previous_cleaned": True})
            for identifier, failure in self.failures().items():
                timestamp = failure.get("timestamp")
                if identifier not in protected and timestamp is not None and now - timestamp >= DIAGNOSTIC_RETENTION:
                    self.delete_database_files(self.database(identifier).parent)
            quarantine = self.data / ".failed"
            if quarantine.is_symlink():
                raise ValueError("Invalid failed database directory")
            for directory in quarantine.glob("*"):
                if not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                    continue
                if now - directory.lstat().st_mtime >= DIAGNOSTIC_RETENTION:
                    self.delete_database_files(directory)
                    try:
                        directory.rmdir()
                    except OSError as error:
                        if error.errno != errno.ENOTEMPTY:
                            raise
                    sync_directory(quarantine)
        except (OSError, ValueError) as error:
            atomic_json(self.cleanup_error, {"reason": str(error), "timestamp": now})
            raise
        if self.cleanup_error.exists():
            remove(self.cleanup_error)

    def try_cleanup(self):
        try:
            self.cleanup()
        except (OSError, ValueError) as error:
            print(f"Database cleanup needs a retry: {error}", file=sys.stderr)

    def quarantine(self, identifier):
        if identifier in self.protected():
            raise RuntimeError("Cannot reuse a deployment directory referenced by a switch")
        source = self.database(identifier).parent
        if not source.exists() or not any((source / name).exists() for name in DATABASE_FILES):
            return
        quarantine = self.data / ".failed"
        if quarantine.is_symlink():
            raise ValueError("Invalid failed database directory")
        quarantine.mkdir(exist_ok=True, mode=0o700)
        target = quarantine / secrets.token_hex(16)
        source.rename(target)
        os.utime(target, None)
        sync_directory(target)
        sync_directory(quarantine)
        sync_directory(self.data)
        self.create_data_dir(source)

    def candidate(self, releases, active_tag, *, allow_blocked=False):
        failures = self.failures()
        remaining = list(releases)
        while release := select_release(remaining, active_tag):
            remaining.remove(release)
            try:
                manifest = release_manifest(release)
            except HTTPError as error:
                if error.code not in (404, 410):
                    raise
                print(f"Rejected {release['tag_name']}: required metadata is unavailable", file=sys.stderr)
                continue
            except (ValueError, KeyError, TypeError) as error:
                print(f"Rejected {release['tag_name']}: {error}", file=sys.stderr)
                continue
            identifier = f"{manifest['tag']}-{manifest['commit'][:12]}"
            if failures.get(identifier, {}).get("blocked") and not allow_blocked:
                continue
            if not supports_upgrade(manifest, active_tag):
                print(f"Rejected {manifest['tag']}: no direct upgrade from {active_tag}", file=sys.stderr)
                continue
            return manifest
        return None

    def deploy(self, manifest):
        tag, commit = manifest["tag"], manifest["commit"]
        identifier = f"{tag}-{commit[:12]}"
        self.quarantine(identifier)
        self.apply(self.prepare(commit, tag))
        failures = self.failures()
        if identifier in failures:
            del failures[identifier]
            atomic_json(self.blocked, failures)

    def retry(self, identifier):
        self.recover()
        if not self.failures().get(identifier, {}).get("blocked") or not ID.fullmatch(identifier):
            raise ValueError("Choose a blocked release ID from the failure records")
        tag = identifier.rsplit("-", 1)[0]
        active = self.active()
        manifest = self.candidate([github("/releases/tags/" + tag)], active["tag"], allow_blocked=True)
        if not manifest or f"{tag}-{manifest['commit'][:12]}" != identifier:
            raise ValueError("The blocked release no longer matches an eligible published release")
        self.deploy(manifest)

    def defer(self, error):
        attempts = read_json(self.backoff)["attempts"] + 1 if self.backoff.exists() else 1
        delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
        atomic_json(
            self.backoff,
            {"attempts": attempts, "until": time.time() + delay, "reason": str(error)},
        )
        print(f"Update check deferred for {delay} seconds: {error}", file=sys.stderr)

    def update(self):
        self.recover()
        active = self.active()
        if not active:
            raise RuntimeError("Install Agent Journal before checking for updates")
        self.try_cleanup()
        if self.backoff.exists() and read_json(self.backoff)["until"] > time.time():
            print("Update check is in backoff.")
            return
        manifest = None
        try:
            manifest = self.candidate(published_releases(), active["tag"])
            if manifest:
                self.deploy(manifest)
            else:
                print("No eligible server release.")
        except (OSError, subprocess.SubprocessError, ReadinessError) as error:
            permanent = isinstance(error, ReadinessError) and not error.transient
            if manifest:
                identifier = manifest["tag"] + "-" + manifest["commit"][:12]
                permanent |= bool(self.failures().get(identifier, {}).get("blocked"))
            if not permanent and not self.pending.exists():
                self.defer(error)
            raise
        if self.backoff.exists():
            remove(self.backoff)

    def status(self):
        print(json.dumps({
            "active": self.active(),
            "failed_releases": self.failures(),
            "pending": read_json(self.pending) if self.pending.exists() else None,
            "backoff": read_json(self.backoff) if self.backoff.exists() else None,
            "cleanup_error": read_json(self.cleanup_error) if self.cleanup_error.exists() else None,
        }, indent=2))


def find_nix():
    for path in (
        shutil.which("nix"),
        "/nix/var/nix/profiles/default/bin/nix",
        "/run/current-system/sw/bin/nix",
    ):
        if path and Path(path).is_file():
            return path
    raise RuntimeError("Install Nix before deploying Agent Journal")


def write_unit(deployment, name):
    path = Path("/etc/systemd/system") / name
    template = deployment.root / "units" / name
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output_file:
        output_file.write(template.read_text(encoding="utf-8"))
        output_file.flush()
        os.fsync(output_file.fileno())
    temporary.chmod(0o644)
    temporary.replace(path)
    sync_directory(path.parent)


def write_controls(deployment, checkout):
    probe = deployment.config / "deployment-probe.env"
    if not probe.exists():
        with probe.open("x", encoding="utf-8") as file:
            file.write(f"AGENT_JOURNAL_DEPLOYMENT_KEY={secrets.token_urlsafe(32)}\n")
            file.flush()
            os.fsync(file.fileno())
        sync_directory(deployment.config)
    templates = deployment.root / "units"
    templates.mkdir(exist_ok=True, mode=0o755)
    for name in (UNIT, RECOVERY_UNIT, UPDATE_SERVICE, UPDATE_TIMER):
        source = (checkout / "deploy" / name).read_text(encoding="utf-8")
        source = source.replace("/usr/bin/python3", json.dumps(sys.executable))
        (templates / name).write_text(source, encoding="utf-8")
    script = deployment.root / "server_deploy.py"
    source = checkout / "scripts" / "server_deploy.py"
    if source.resolve() != script.resolve():
        temporary = deployment.root / ".server-deploy.py.tmp"
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        temporary.replace(script)
        sync_directory(deployment.root)
    write_unit(deployment, UNIT)
    write_unit(deployment, RECOVERY_UNIT)
    if (Path("/etc/systemd/system") / UPDATE_TIMER).exists():
        write_unit(deployment, UPDATE_SERVICE)
        write_unit(deployment, UPDATE_TIMER)
    run("systemctl", "daemon-reload")


def install_controls(deployment, checkout):
    with deployment.lock():
        if deployment.pending.exists() or deployment.maintenance.exists():
            raise RuntimeError("Recover the pending deployment before refreshing host controls")
        write_controls(deployment, checkout)
    # Later service restarts can use the completed one-shot without taking the update lock.
    run("systemctl", "start", RECOVERY_UNIT)


def automatic_updates(deployment, enabled):
    if not deployment.active():
        raise RuntimeError("Install Agent Journal before configuring its release watcher")
    if enabled:
        for name in (UPDATE_SERVICE, UPDATE_TIMER):
            write_unit(deployment, name)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", UPDATE_TIMER)
    elif (Path("/etc/systemd/system") / UPDATE_TIMER).exists():
        run("systemctl", "disable", "--now", UPDATE_TIMER)


def install(deployment, domain, tag=None, local=False, automatic=False):
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
    if local:
        commit = output("git", "-C", str(checkout), "rev-parse", "HEAD")
    else:
        tag, commit = install_release(tag)
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
    sync_directory(deployment.root.parent)
    sync_directory(deployment.data.parent)
    CONFIG.mkdir(exist_ok=True, mode=0o700)
    token_path = CONFIG / "server.env"
    if not token_path.exists():
        with token_path.open("x", encoding="utf-8") as handle:
            handle.write(f"AGENT_JOURNAL_TOKEN={secrets.token_urlsafe(32)}\n")
    atomic_json(deployment.root / "host.json", {"nix": nix})
    # systemd and the service user need to traverse this directory.
    deployment.root.chmod(0o755)
    deployment.data.chmod(0o711)
    install_controls(deployment, checkout)
    with deployment.lock():
        identifier = deployment.prepare(
            commit, tag, checkout=checkout if local else None
        )
        deployment.apply(identifier, legacy=not local)
        run("systemctl", "enable", UNIT)
        if automatic:
            automatic_updates(deployment, True)
    if proxy:
        install_proxy(proxy, domain, checkout, Path("/etc/systemd/system"))
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
    installer = commands.add_parser("install")
    installer.add_argument("--domain")
    installer.add_argument("--auto-update", action="store_true", help="enable scheduled release updates")
    source = installer.add_mutually_exclusive_group()
    source.add_argument(
        "--release", metavar="vX.Y.Z", help="install a specific stable release"
    )
    source.add_argument(
        "--local", action="store_true", help="install from the checkout for testing"
    )
    commands.add_parser("update")
    commands.add_parser("recover")
    commands.add_parser("boot-recover")
    commands.add_parser("cleanup")
    commands.add_parser("status")
    commands.add_parser("install-controls", help="refresh the host updater from a reviewed checkout")
    commands.add_parser("retry").add_argument("identifier")
    commands.add_parser("auto-update").add_argument("mode", choices=("enable", "disable"))
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux":
        parser.error("Run as root on a Linux VM with systemd")
    os.umask(0o077)
    deployment = Deployment()
    try:
        if args.command == "install":
            install(deployment, args.domain, args.release, args.local, args.auto_update)
        elif args.command == "install-controls":
            if not deployment.active():
                raise RuntimeError("Install Agent Journal before refreshing its host controls")
            install_controls(deployment, Path(__file__).resolve().parents[1])
        else:
            with deployment.lock():
                if args.command == "retry":
                    deployment.retry(args.identifier)
                elif args.command == "auto-update":
                    automatic_updates(deployment, args.mode == "enable")
                else:
                    getattr(deployment, args.command.replace("-", "_"))()
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
