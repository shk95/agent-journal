import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.server_deploy import (
    CONFIG,
    Deployment,
    release_commit,
    select_release,
)

OLD = "bootstrap-" + "0" * 12
GOOD = "server-v0.2.0-" + "1" * 12
BAD = "server-v0.3.0-" + "2" * 12


def release(tag, *, prerelease=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": False, "assets": []}


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
                    gate.unlink()
                    with urlopen(request, timeout=2) as response:
                        self.assertEqual(json.load(response), [])
                finally:
                    server.shutdown()

    def test_selects_highest_new_server_release(self):
        releases = [
            release("client-v4.0.0"),
            release("server-v0.10.0"),
            release("server-v0.9.0"),
            release("server-v0.11.0", prerelease=True),
        ]
        self.assertEqual(select_release(releases, "server-v0.9.0"), releases[1])
        self.assertIsNone(select_release(releases, "server-v0.10.0"))

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
            with patch.object(deployment, "create_data_dir"):
                deployment.prepare("0" * 40, checkout=checkout)
            deployment.point_at(OLD)
            self.assertEqual(deployment.active()["tag"], "server-v0.9.0")
            self.assertIsNone(
                select_release([release("server-v0.8.0")], deployment.active()["tag"])
            )

    def test_release_manifest_matches_digest_and_tag(self):
        entry = release("server-v0.2.0")
        commit = "1" * 40
        body = json.dumps({"commit": commit, "tag": entry["tag_name"]}).encode()
        entry["assets"].append(
            {
                "name": "server-release.json",
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
        )
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

    def test_interrupted_switch_restores_prior_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = Deployment(root, root / "data")
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
            self.assertEqual(json.loads(deployment.blocked.read_text()), [BAD])
            self.assertEqual(
                [call.args[0] for call in service.call_args_list], ["stop", "start"]
            )
            wait.assert_called_with(OLD)


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
        deployment.apply(candidate(GOOD, "server-v0.2.0", "1" * 40))
        add_note("after-update")
        try:
            deployment.apply(candidate(BAD, "server-v0.3.0", "2" * 40))
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
        assert BAD in json.loads(deployment.blocked.read_text())
    print("Initial systemd startup, update, write gate, and rollback passed.")


if __name__ == "__main__":
    if sys.argv[1:] == ["smoke"]:
        smoke()
    else:
        unittest.main()
