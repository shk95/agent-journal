import hashlib
import errno
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.server_deploy import (
    CONFIG,
    MANIFEST,
    Deployment,
    ReadinessError,
    atomic_json,
    host_setting,
    install_release,
    release_commit,
    select_release,
)

OLD = "bootstrap-" + "0" * 12
GOOD = "v0.2.0-" + "1" * 12
BAD = "v0.3.0-" + "2" * 12
MIGRATION = "v0.4.1-" + "3" * 12
STARTUP = "v0.4.2-" + "4" * 12
CLEANUP = "v0.4.3-" + "5" * 12
UNCOMMITTED = "v0.4.4-" + "6" * 12
COMMITTED = "v0.4.5-" + "7" * 12
READINESS = "v0.4.6-" + "8" * 12


def release(tag, *, prerelease=False, manifest=True):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": False,
        "assets": [{"name": MANIFEST}] if manifest else [],
    }


class DeploymentTests(unittest.TestCase):
    def test_maintenance_rejects_requests_but_health_identifies_candidate(self):
        from agent_journal.server import make_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = root / "maintenance"
            with make_server(
                "127.0.0.1",
                0,
                root / "journal.sqlite3",
                "fixture",
                deployment_id=GOOD,
                maintenance_file=gate,
                deployment_probe_key="fixture-probe",
            ) as server:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    gate.touch()
                    with urlopen(base + "/healthz", timeout=2) as response:
                        self.assertEqual(
                            json.load(response), {"status": "ready", "deployment": GOOD}
                        )
                    request = Request(
                        base + "/api/projects",
                        headers={"Authorization": "Bearer fixture"},
                    )
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=2)
                    self.assertEqual(error.exception.code, 503)
                    probe = Request(
                        base + "/internal/deployment",
                        headers={"Authorization": "Bearer fixture"},
                    )
                    with self.assertRaises(HTTPError) as error:
                        urlopen(probe, timeout=2)
                    self.assertEqual(error.exception.code, 401)
                    probe.add_header("X-Journal-Deployment-Key", "fixture-probe")
                    with urlopen(probe, timeout=2) as response:
                        self.assertEqual(json.load(response), {"status": "ready", "deployment": GOOD})
                    gate.unlink()
                    with urlopen(request, timeout=2) as response:
                        self.assertEqual(json.load(response), [])
                finally:
                    server.shutdown()

    def test_selects_highest_new_server_release(self):
        releases = [
            release("client-v4.0.0"),
            release("v0.10.0"),
            release("v0.9.0"),
            release("v0.11.0", prerelease=True),
            release("v0.12.0", manifest=False),
        ]
        self.assertEqual(select_release(releases, "server-v0.9.0"), releases[1])
        self.assertIsNone(select_release(releases, "v0.10.0"))

    def test_bootstrap_selects_a_published_stable_release(self):
        stable = release("v0.2.0")
        preview = release("v0.3.0", prerelease=True)
        with (
            patch(
                "scripts.server_deploy.github",
                side_effect=[[preview, stable], stable, preview],
            ) as github,
            patch(
                "scripts.server_deploy.release_commit", return_value="1" * 40
            ) as commit,
        ):
            self.assertEqual(install_release(None), ("v0.2.0", "1" * 40))
            self.assertEqual(install_release("v0.2.0"), ("v0.2.0", "1" * 40))
            with self.assertRaisesRegex(ValueError, "No published stable"):
                install_release("v0.3.0")
            github.assert_any_call("/releases/tags/v0.2.0")
            commit.assert_called_with(stable)

    def test_bootstrap_uses_package_version_as_update_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text('[project]\nversion="0.9.0"\n')
            deployment = Deployment(root, root / "data")
            executable = deployment.slot(OLD) / "package/bin/agent-journal"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(deployment, "create_data_dir", side_effect=lambda path: path.mkdir(parents=True)):
                deployment.prepare("0" * 40, checkout=checkout)
            deployment.point_at(OLD)
            self.assertEqual(deployment.active()["tag"], "v0.9.0")
            self.assertIsNone(
                select_release([release("v0.8.0")], deployment.active()["tag"])
            )

    def test_release_manifest_matches_digest_and_tag(self):
        entry = release("v0.2.0")
        commit = "1" * 40
        body = json.dumps({"commit": commit, "tag": entry["tag_name"]}).encode()
        entry["assets"][0]["digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
        with (
            patch("scripts.server_deploy.fetch", return_value=body),
            patch(
                "scripts.server_deploy.github",
                return_value={"object": {"type": "commit", "sha": commit}},
            ),
        ):
            self.assertEqual(release_commit(entry), commit)
        with patch("scripts.server_deploy.fetch", return_value=b"edited"):
            with self.assertRaisesRegex(ValueError, "digest"):
                release_commit(entry)
        body = json.dumps({
            "commit": commit, "tag": entry["tag_name"], "upgrade_from": None,
        }).encode()
        entry["assets"][0]["digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
        with (
            patch("scripts.server_deploy.fetch", return_value=body),
            patch(
                "scripts.server_deploy.github",
                return_value={"object": {"type": "commit", "sha": commit}},
            ),
            self.assertRaisesRegex(ValueError, "source ranges"),
        ):
            release_commit(entry)

    def test_interrupted_switch_restores_prior_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
            deployment.database(OLD).parent.mkdir(parents=True)
            deployment.database(OLD).touch()
            deployment.point_at(BAD)
            deployment.pending.write_text(
                json.dumps({"previous": OLD, "candidate": BAD})
            )
            with (
                patch.object(deployment, "service") as service,
                patch.object(deployment, "wait_ready") as wait,
                patch.object(deployment, "active", return_value={"id": OLD}),
            ):
                deployment.recover()
            self.assertEqual(deployment.current.readlink(), deployment.slot(OLD))
            self.assertFalse(deployment.pending.exists())
            self.assertFalse(deployment.maintenance.exists())
            self.assertFalse(deployment.failures()[BAD]["blocked"])
            self.assertEqual(deployment.failures()[BAD]["phase"], "switch")
            self.assertEqual(
                [call.args[0] for call in service.call_args_list], ["stop", "start"]
            )
            wait.assert_called_with(OLD)

    def test_excludes_blocked_and_incompatible_releases_before_deploying(self):
        highest = "v0.5.0"
        incompatible = "v0.4.0"
        eligible = "v0.2.0"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
            deployment.blocked.write_text(json.dumps([highest + "-" + "a" * 12]))

            def manifest(entry):
                tag = entry["tag_name"]
                commit_initial = {highest: "a", incompatible: "b", eligible: "1"}[tag]
                low = "v0.3.0" if tag == incompatible else "v0.1.0"
                return {
                    "tag": tag, "commit": commit_initial * 40,
                    "upgrade_from": [{"minimum": low, "maximum": tag}],
                }

            with patch("scripts.server_deploy.release_manifest", side_effect=manifest):
                result = deployment.candidate(
                    [release(name) for name in (eligible, incompatible, highest)], "v0.1.0"
                )
            self.assertEqual(result["tag"], eligible)

    def test_boot_recovers_both_sides_of_commit_without_starting_services(self):
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                deployment = Deployment(root, root / "data")
                for identifier in (OLD, GOOD):
                    deployment.database(identifier).parent.mkdir(parents=True)
                    deployment.database(identifier).touch()
                    deployment.slot(identifier).mkdir(parents=True)
                    atomic_json(deployment.slot(identifier) / "release.json", {"id": identifier})
                deployment.point_at(GOOD)
                state = {"previous": OLD, "candidate": GOOD, "switch": "a" * 32, "phase": "commit"}
                atomic_json(deployment.pending, state)
                deployment.fence()
                if committed:
                    deployment.commits.mkdir()
                    atomic_json(deployment.commits / ("a" * 32 + ".json"), state)
                with patch.object(deployment, "service") as service:
                    deployment.boot_recover()
                    deployment.boot_recover()
                    service.assert_not_called()
                self.assertEqual(deployment.active()["id"], GOOD if committed else OLD)
                self.assertTrue(deployment.database(OLD).exists())
                self.assertFalse(deployment.maintenance.exists())

    def test_cleanup_failure_keeps_committed_candidate_active_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
            for identifier in (OLD, GOOD):
                deployment.database(identifier).parent.mkdir(parents=True)
                deployment.slot(identifier).mkdir(parents=True)
                atomic_json(deployment.slot(identifier) / "release.json", {"id": identifier})
            deployment.database(OLD).write_text("old fixture")
            deployment.point_at(OLD)
            with (
                patch.object(deployment, "service"),
                patch.object(deployment, "wait_ready"),
                patch.object(deployment, "check_application"),
                patch.object(deployment, "snapshot", side_effect=lambda _, ident:
                    deployment.database(ident).write_text("new fixture")),
                patch.object(deployment, "delete_database_files", side_effect=OSError("fixture error")),
            ):
                deployment.apply(GOOD)
            self.assertEqual(deployment.active()["id"], GOOD)
            self.assertFalse(deployment.maintenance.exists())
            self.assertTrue(deployment.database(OLD).exists())
            self.assertTrue(deployment.cleanup_error.exists())
            deployment.cleanup()
            deployment.cleanup()
            self.assertFalse(deployment.database(OLD).exists())
            self.assertTrue(deployment.database(GOOD).exists())
            self.assertFalse(deployment.cleanup_error.exists())

    def test_retry_preserves_failed_copy_and_uses_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
            failed = deployment.database(BAD)
            failed.parent.mkdir(parents=True)
            failed.write_text("failed fixture")
            deployment.record_failure(
                BAD, {"phase": "migration", "reason": "fixture", "timestamp": 0}, True,
            )
            manifest = {
                "tag": "v0.3.0", "commit": "2" * 40,
                "upgrade_from": [{"minimum": "v0.2.0", "maximum": "v0.2.0"}],
            }
            with (
                patch.object(deployment, "active", return_value={"id": GOOD, "tag": "v0.2.0"}),
                patch("scripts.server_deploy.github", return_value=release("v0.3.0")),
                patch("scripts.server_deploy.release_manifest", return_value=manifest),
                patch.object(deployment, "prepare", return_value=BAD),
                patch.object(deployment, "apply") as apply,
                patch.object(deployment, "create_data_dir", side_effect=lambda path: path.mkdir()),
            ):
                deployment.retry(BAD)
                apply.assert_called_once_with(BAD)
            self.assertFalse(failed.exists())
            quarantined = list((deployment.data / ".failed").glob("*/journal.sqlite3"))
            self.assertEqual([path.read_text() for path in quarantined], ["failed fixture"])
            self.assertNotIn(BAD, deployment.failures())

    def test_transient_discovery_uses_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
            with (
                patch.object(deployment, "active", return_value={"id": GOOD, "tag": "v0.2.0"}),
                patch("scripts.server_deploy.published_releases", side_effect=OSError(errno.EIO, "fixture")) as fetch,
                self.assertRaises(OSError),
            ):
                deployment.update()
            self.assertEqual(json.loads(deployment.backoff.read_text())["attempts"], 1)
            with (
                patch.object(deployment, "active", return_value={"id": GOOD, "tag": "v0.2.0"}),
                patch("scripts.server_deploy.published_releases") as fetch,
            ):
                deployment.update()
                fetch.assert_not_called()


def smoke():
    deployment = Deployment()
    token = (CONFIG / "server.env").read_text().strip().split("=", 1)[1]

    def api(path, data=None):
        req = Request(
            "http://127.0.0.1:8765" + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def add_note(identifier):
        api(
            "/api/notes",
            {"project": "example/fixture", "id": identifier, "body": "Fixture note"},
        )

    with deployment.lock():
        old = deployment.active()["id"]
        if not old.startswith("bootstrap-") or api("/api/projects"):
            raise RuntimeError(
                "Run this smoke test on a fresh, disposable Linux installation"
            )
        api("/api/register", {"project": "example/fixture"})
        add_note("before-update")
        store = (deployment.slot(old) / "package").resolve()

        def candidate(identifier, tag, commit):
            slot = deployment.slot(identifier)
            slot.mkdir()
            (slot / "package").symlink_to(store)
            return deployment.prepare(commit, tag)

        normal_health = deployment.wait_ready

        def check(identifier):
            normal_health(identifier)
            try:
                add_note("during-update")
            except HTTPError as error:
                assert error.code == 503, error.code
            else:
                raise AssertionError(
                    "Accepted a write while the update gate was active"
                )
            if identifier == BAD:
                raise RuntimeError("Intentional health-check failure")

        deployment.wait_ready = check
        deployment.apply(candidate(GOOD, "v0.2.0", "1" * 40))
        add_note("after-update")
        try:
            deployment.apply(candidate(BAD, "v0.3.0", "2" * 40))
        except RuntimeError as error:
            assert str(error) == "Intentional health-check failure"
        else:
            raise AssertionError("The simulated failure did not trigger rollback")
        assert api("/healthz")["deployment"] == GOOD
        assert [note["id"] for note in api("/api/notes?project=example/fixture")] == [
            "before-update",
            "after-update",
        ]
        assert not deployment.maintenance.exists()
        assert deployment.failures()[BAD]["blocked"]
    print("Initial systemd startup, update, write gate, and rollback passed.")


def extended_smoke():
    deployment = Deployment()
    with deployment.lock():
        if deployment.active()["id"] != GOOD:
            raise RuntimeError("Run the initial disposable deployment smoke test first")
        store = (deployment.slot(GOOD) / "package").resolve()
        snapshot = deployment.snapshot
        wait_ready = deployment.wait_ready

        def prepare(identifier, *, broken_start=False):
            tag, digest = identifier.rsplit("-", 1)
            slot = deployment.slot(identifier)
            slot.mkdir(exist_ok=True)
            deployment.quarantine(identifier)
            if broken_start:
                command = slot / "package/bin/agent-journal"
                command.parent.mkdir(parents=True, exist_ok=True)
                command.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
                command.chmod(0o755)
            elif not (slot / "package").is_symlink():
                (slot / "package").symlink_to(store)
            return deployment.prepare(digest[0] * 40, tag)

        def corrupt_copy(previous, identifier):
            snapshot(previous, identifier)
            deployment.database(identifier).write_bytes(b"Invalid synthetic SQLite database")

        prepare(MIGRATION)
        with (
            patch.object(deployment, "snapshot", side_effect=corrupt_copy),
            patch.object(deployment, "wait_ready", side_effect=lambda identifier: wait_ready(identifier, timeout=8)),
        ):
            try:
                deployment.apply(MIGRATION)
            except ReadinessError as error:
                assert error.phase == "migration", error
            else:
                raise AssertionError("Corrupt database was accepted")
        prepare(STARTUP, broken_start=True)
        with patch.object(deployment, "wait_ready", side_effect=lambda identifier: wait_ready(identifier, timeout=6)):
            try:
                deployment.apply(STARTUP)
            except (ReadinessError, subprocess.SubprocessError):
                assert deployment.failures()[STARTUP]["phase"] == "startup"
            else:
                raise AssertionError("Failed process startup was accepted")
        assert deployment.active()["id"] == GOOD
        assert deployment.database(GOOD).exists()

        def interrupt_cleanup(path):
            assert path == deployment.database(GOOD).parent
            (path / "journal.sqlite3-shm").unlink(missing_ok=True)
            raise InterruptedError("Synthetic cleanup interruption")

        prepare(CLEANUP)
        with patch.object(deployment, "delete_database_files", side_effect=interrupt_cleanup):
            deployment.apply(CLEANUP)
        assert deployment.active()["id"] == CLEANUP and not deployment.maintenance.exists()
        assert deployment.cleanup_error.exists() and deployment.database(GOOD).exists()
        deployment.check_application(CLEANUP)
        deployment.cleanup()
        assert not deployment.database(GOOD).exists()
        assert deployment.database(CLEANUP).exists() and not deployment.cleanup_error.exists()
    print("Migration and startup rollback, plus interrupted database cleanup, passed.")


def stage_boot_smoke(committed):
    deployment = Deployment()
    identifier = COMMITTED if committed else UNCOMMITTED
    with deployment.lock():
        if deployment.active()["id"] != CLEANUP:
            raise RuntimeError("Run the disposable deployment failure smoke test first")
        store = (deployment.slot(CLEANUP) / "package").resolve()
        slot = deployment.slot(identifier)
        slot.mkdir(exist_ok=True)
        deployment.quarantine(identifier)
        if not (slot / "package").is_symlink():
            (slot / "package").symlink_to(store)
        tag, digest = identifier.rsplit("-", 1)
        assert deployment.prepare(digest[0] * 40, tag) == identifier
        state = {
            "previous": CLEANUP, "candidate": identifier,
            "switch": secrets.token_hex(16), "phase": "startup",
        }
        atomic_json(deployment.pending, state)
        deployment.fence()
        deployment.service("stop")
        deployment.snapshot(CLEANUP, identifier)
        deployment.point_at(identifier)
        if committed:
            deployment.service("start")
            deployment.wait_ready(identifier)
            deployment.check_application(identifier)
            deployment.set_phase(state, "commit")
            atomic_json(
                deployment.commits / (state["switch"] + ".json"),
                state | {"timestamp": time.time(), "previous_cleaned": False},
            )
    print(f"Staged the {'committed' if committed else 'uncommitted'} switch for a reboot.")


def verify_boot_smoke(committed):
    deployment = Deployment()
    identifier = COMMITTED if committed else CLEANUP
    deployment.wait_ready(identifier)
    deployment.check_application(identifier)
    assert deployment.active()["id"] == identifier
    assert not deployment.pending.exists()
    assert not deployment.maintenance.exists()
    assert deployment.database(CLEANUP).exists()
    print(f"Recovered the {'committed' if committed else 'uncommitted'} switch at boot.")


def readiness_smoke():
    deployment = Deployment()
    with deployment.lock():
        if deployment.active()["id"] != COMMITTED:
            raise RuntimeError("Run the disposable committed boot smoke test first")
        slot = deployment.slot(READINESS)
        slot.mkdir()
        (slot / "package").symlink_to((deployment.slot(COMMITTED) / "package").resolve())
        assert deployment.prepare("8" * 40, "v0.4.6") == READINESS

        def bad_key(path, name):
            if name == "AGENT_JOURNAL_DEPLOYMENT_KEY":
                return "wrong-fixture-probe-key"
            return host_setting(path, name)

        with patch("scripts.server_deploy.host_setting", side_effect=bad_key):
            try:
                deployment.apply(READINESS)
            except ReadinessError as error:
                assert error.phase == "readiness" and "401" in str(error), error
            else:
                raise AssertionError("Deployment passed with an invalid operator probe key")
        assert deployment.active()["id"] == COMMITTED
        assert deployment.database(COMMITTED).exists()
        assert deployment.failures()[READINESS]["blocked"]
        assert not deployment.maintenance.exists()
        deployment.check_application(COMMITTED)
    print("Failed authenticated readiness check restored the predecessor database.")


if __name__ == "__main__":
    if sys.argv[1:] == ["smoke"]:
        smoke()
    elif sys.argv[1:] == ["smoke-failures"]:
        extended_smoke()
    elif sys.argv[1:] in (["stage-boot-uncommitted"], ["stage-boot-committed"]):
        stage_boot_smoke(sys.argv[1] == "stage-boot-committed")
    elif sys.argv[1:] in (["verify-boot-uncommitted"], ["verify-boot-committed"]):
        verify_boot_smoke(sys.argv[1] == "verify-boot-committed")
    elif sys.argv[1:] == ["smoke-readiness"]:
        readiness_smoke()
    else:
        unittest.main()
