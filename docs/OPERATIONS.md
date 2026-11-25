# Cloud operations

Run the central server on a Linux VM with a persistent volume and an HTTPS reverse proxy. The examples use systemd and Caddy. Install Agent Journal from a reviewed Git revision before creating the service.

## Managed installation

Use a Linux VM with a running systemd instance, Git, Python 3.11 or newer, and a working Nix installation. The installer needs sudo. For managed HTTPS, install Caddy, point a public hostname at the VM, and leave ports 80 and 443 available for the journal proxy.

```sh
git clone https://github.com/shk95/agent-journal.git
cd agent-journal
sudo python3 scripts/server_deploy.py install --domain journal.example.com
```

Omit `--domain` if HTTPS is already handled by an existing proxy. Route that proxy to `127.0.0.1:8765`. The installer builds the checkout's `#server` package, starts the journal, and enables the release timer. Verify the public HTTPS address after installation and confirm that the local health check identifies the running deployment.

```sh
curl -f http://127.0.0.1:8765/healthz
systemctl status agent-journal.service
systemctl list-timers agent-journal-update.timer
```

The installer generates the access token in `/etc/agent-journal/server.env`. Share it through a private channel when connecting a work machine. Deployed packages and the updater live under `/opt/agent-journal`. The active database is reached through `/opt/agent-journal/current/data/journal.sqlite3` and stored on the persistent volume under `/var/lib/agent-journal`.

An existing manually managed `agent-journal.service` requires a separate migration. Take and verify a backup before changing ownership of the service or moving its database.

## Publish a release

Run the [local checks](SETUP.md#local-development) before tagging. On `master`, set the shared server and client version in `pyproject.toml` and update `uv.lock`. Push a tag that matches that version.

```sh
git tag v0.2.0
git push origin v0.2.0
```

The release workflow checks the public history, builds the Nix outputs, and verifies their CLI entry points. On success it publishes one GitHub Release for both packages and a server update manifest containing the exact commit. A manual run of the Release workflow verifies packaging without publishing a tag. Consider enabling [immutable releases](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes) before publishing the first release.

Use the same Git tag for either Nix output. For example, `nix run 'github:shk95/agent-journal/v0.2.0#client' -- tui` runs that release's client.

The timer checks every 15 minutes for a stable `vX.Y.Z` release with a server manifest and a version newer than the installed package. It validates the manifest digest and verifies that the Git tag points to the manifest's commit. It builds that commit while the old service is running. A public repository allows the VM to check for updates without GitHub credentials.

For the switch, the updater fences client requests, stops the old process, and snapshots its database into the new deployment. Requests resume after the new process reports the expected deployment ID and a ready database. On failure the updater restores the previous executable and its original database. It records failed revisions so the timer will wait for a corrected release. An interrupted switch is reconciled when the updater runs again.

```sh
sudo systemctl start agent-journal-update.service
sudo journalctl -u agent-journal-update.service -n 100 --no-pager
sudo python3 /opt/agent-journal/server_deploy.py recover
```

The updater and systemd unit files are installed as host configuration. Replacing those control files requires a reviewed host change. Application releases update the server package and web UI while using the host token and proxy settings. Keep disk space for the candidate and the prior database snapshot. Use independent backups for recovery from storage loss.

If a VM has an updater from the `server-v` tag era, refresh the installed script from a reviewed checkout before publishing the first shared release.

```sh
sudo systemctl stop agent-journal-update.timer agent-journal-update.service
sudo install -m 0600 scripts/server_deploy.py /opt/agent-journal/server_deploy.py
sudo python3 /opt/agent-journal/server_deploy.py recover
sudo systemctl start agent-journal-update.timer
```

## Manual installation

### Server

Create a service account called `journal`. Give it read access to the checked-out code or installed package, and exclusive write access to its data directory. Keep the service environment file outside the checkout with permissions `0600`. Put one generated `AGENT_JOURNAL_TOKEN` in that file.

The following unit assumes an executable installed at `/opt/agent-journal/.venv/bin/agent-journal`. Adjust the local paths when installing.

```ini
[Unit]
Description=Agent Journal server
After=network.target

[Service]
User=journal
Group=journal
EnvironmentFile=/etc/agent-journal.env
StateDirectory=agent-journal
StateDirectoryMode=0700
UMask=0077
ExecStart=/opt/agent-journal/.venv/bin/agent-journal serve --database /var/lib/agent-journal/journal.sqlite3
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Keep the application bound to loopback and expose only the HTTPS proxy. Point the domain at the VM and configure Caddy to terminate TLS.

```caddyfile
journal.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Start both services and check that they restart on boot. From each work machine run `agent-journal connect --url https://journal.example.com`, register a repository on one machine, then start its observer and the observer on the second machine. Verify that `agent-journal projects` lists the same registration on both machines and that the browser accepts the token over HTTPS.

## Observers

Run one `agent-journal observe` per work machine as the same user that runs Codex. The process needs access to the shared Codex App Server socket and the saved journal connection. Give the observer service the installed Codex and Agent Journal binaries on its PATH.

An observer can start before Codex. It retries while the socket or network is unavailable. For unattended use, run the observer in your user service manager with restart on failure. A foreground observer is useful for seeing connection and summary errors during setup.

## Backups and restore

Create the backup on a private filesystem with sufficient disk space. The snapshot command can run while the journal is serving.

For a managed installation, use `/opt/agent-journal/current/data/journal.sqlite3` as the database path and `/opt/agent-journal/current/package/bin/agent-journal` as the executable.

```sh
agent-journal backup --database /var/lib/agent-journal/journal.sqlite3 --output /path/to/private/backup.sqlite3
```

Copy the backup to protected storage. It contains transcripts and notes. To verify it, start a separate test instance against a private copy at a new database path. Check that known turns and notes load. To restore for normal use, point the stopped application service at the verified copy or place it at a new persistent database path and restart the service.

If the access token changes, update the server environment file and restart the service. Run `agent-journal connect` again on each work machine, then restart its observer. Sign in again in the browser.
