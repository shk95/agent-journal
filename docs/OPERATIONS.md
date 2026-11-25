# Cloud operations

Run the central server on a Linux VM with a persistent volume and an HTTPS reverse proxy. The examples use systemd and Caddy. Use a published stable release for the application and the initial host configuration.

## Managed installation

Use a Linux VM with a running systemd instance, Git, curl, Python 3.11 or newer, and a working Nix installation. The installer needs sudo. For managed HTTPS, install Caddy, point a public hostname at the VM, and leave ports 80 and 443 available for the journal proxy.

On a host that regenerates system accounts at boot, declare the `journal` service user and group with persistent IDs in the host configuration before installing.

```sh
release_url=$(curl -fsSL --proto '=https' -o /dev/null -w '%{url_effective}' https://github.com/shk95/agent-journal/releases/latest)
release_tag=${release_url##*/}
git clone --branch "$release_tag" https://github.com/shk95/agent-journal.git
cd agent-journal
sudo python3 scripts/server_deploy.py install --release "$release_tag" --domain journal.example.com
```

Omit `--domain` if HTTPS is already handled by an existing proxy. Route that proxy to `127.0.0.1:8765`. The installer verifies the release manifest and tag, builds the released commit's `#server` package, and starts the journal. Without `--release`, it selects the newest published stable server release. Pass `--auto-update` to install and enable the release timer at the same time. Use `--local` for the disposable VM test in [local development](SETUP.md#local-deployment-smoke-test).

```sh
curl -f http://127.0.0.1:8765/healthz
curl -f https://journal.example.com/healthz
systemctl status agent-journal.service
systemctl status agent-journal-recover.service
```

The installer generates the access token in `/etc/agent-journal/server.env`. Share it through a private channel when connecting a work machine. Deployed packages and the updater live under `/opt/agent-journal`. The active database is reached through `/opt/agent-journal/current/data/journal.sqlite3` and stored on the persistent volume under `/var/lib/agent-journal`.

An existing manually managed `agent-journal.service` requires a separate migration. Take and verify a backup before changing ownership of the service or moving its database.

## Release monitoring and recovery

Use the host updater to check and apply one eligible release manually or to install and enable scheduled updates. Disabling the timer preserves the service, active release, and boot recovery. An update already under way can finish.

```sh
sudo python3 /opt/agent-journal/server_deploy.py update
sudo python3 /opt/agent-journal/server_deploy.py auto-update enable
systemctl list-timers agent-journal-update.timer
sudo python3 /opt/agent-journal/server_deploy.py auto-update disable
```

The timer checks for a newer stable release every 15 minutes. The updater verifies the manifest digest and the Git tag's commit. It selects the highest compatible release whose exact deployment identity is eligible. A blocked newer release leaves compatible intermediate releases eligible.

After building a candidate, the updater holds normal client traffic, stops the predecessor, and snapshots its database. The candidate opens and migrates its copy, reports its identity, and passes an authenticated read through a local operator probe. The updater persists the successful commit before reopening traffic. The boot recovery unit reconciles any pending switch before the application starts, even with scheduled checks disabled.

Confirmed migration, startup, or readiness failures are recorded against the exact release identity after rollback. Connection, download, and host resource errors can be retried after backoff. Use `status` to see the recorded phase, reason, and time. An explicit retry revalidates the release, quarantines a leftover candidate copy when eligible, and takes a new snapshot of the active database.

```sh
sudo python3 /opt/agent-journal/server_deploy.py status
sudo python3 /opt/agent-journal/server_deploy.py retry v0.5.0-0123456789ab
sudo python3 /opt/agent-journal/server_deploy.py recover
sudo python3 /opt/agent-journal/server_deploy.py cleanup
```

Cleanup runs after commit and is independently retryable. It removes eligible predecessor database files and retains failed candidate databases for seven days. Active databases and those referenced by an unfinished switch are protected. With automatic updates disabled, run `cleanup` to expire failed diagnostic copies or retry failed cleanup. Keep independent backups on separate storage to recover data after the new deployment accepts writes.

Refresh the host controls of an existing managed installation from a reviewed checkout containing the desired updater and unit files. The command preserves the existing timer setting. Recover any pending switch first.

```sh
sudo python3 scripts/server_deploy.py install-controls
```

## Publish a release

Agent Journal guarantees client/server compatibility across stable releases with the same major version, including `0.x`. An older client must keep working with a newer server, and a newer client must keep existing features working with an older server. Use a patch release for compatible fixes, a minor release for compatible additions, and a major release for incompatible client/server changes.

Keep existing endpoint paths, required fields, and response semantics stable within a major. Add fields or endpoints without making them mandatory for older clients. When a new client uses an optional server feature, detect support and keep the existing workflow usable on older servers.

On `master`, set the shared server and client version in `pyproject.toml` and update `uv.lock`. Run the [local checks](SETUP.md#local-development) and `just compatibility`. The compatibility check exercises both client/server directions against the stable tags in the same major. Push a tag that matches the candidate version.

```sh
git tag v0.4.0
git push origin v0.4.0
```

The release workflow checks the public history, mixed client/server versions, Nix outputs, and standalone clients. On success it publishes a single GitHub Release containing the server manifest with the exact commit, three standalone client executables, client checksums, and the installer. A manual run of the Release workflow verifies packaging without publishing a tag.

Use the same Git tag for either Nix output. For example, `nix run 'github:shk95/agent-journal/v0.4.0#client' -- tui` runs that release's client.

Declare the tested database source ranges under `[[tool.agent-journal.upgrade-from]]` in `pyproject.toml`. Each entry contains inclusive `minimum` and `maximum` stable tags. The release workflow publishes them as `upgrade_from` in the verified server manifest. Use `uv run --locked python -m tests.compatibility --database-migrations` to test same-major snapshots locally. Keep the ranges limited to tested migrations, including when releasing across a major version.

Legacy releases with no source ranges are available for an explicit fresh install, including recovery into an independent deployment from a verified backup. Automated and one-shot in-place updates require a compatible range. Refresh the host controls when changing the updater itself. Application releases preserve the server token, proxy settings, and updater schedule.

## Test cleanup policy

Local smoke, performance, and compatibility runs use private temporary workspaces. After their processes have stopped, the runners delete the workspace on success, failure, or cancellation. On failure they save the traceback and, when available, the last 64 KiB of the isolated collector log in `.local/test-failures/`. The report directory is private. The retention policy covers these reports. Fixture databases, connection settings, browser profiles, and interactive demo data stay outside the report directory.

Reports expire after 24 hours. The next local integration run removes expired reports, or an operator can run `just clean-test-reports` from the checkout. Collect any diagnostics needed for a longer investigation in private storage before expiry. Review reports for local paths and credentials before sharing. Confirm the disposition of interactive demo data separately, since an interrupted demo may contain notes written by its user.

On a workstation, stop tests with `Ctrl+C` or `SIGTERM` and let the runner finish cleanup. If the runner itself must be force-killed, the supervising job owns cleanup. Use a disposable VM or a supervisor that can stop the entire job scope, including descendants that started their own sessions. For a run already interrupted on a shared workstation, verify the run's private directory and its process IDs or socket before stopping residual processes.

Run the deployment scenarios on [one disposable Linux VM](SETUP.md#local-deployment-smoke-test). Keep its services and disk through the rollback and boot-recovery checks. Retire the VM and its test disk after the last check or when abandoning the run. If a failure needs investigation, keep the VM isolated until the relevant diagnostics are copied, then retire it. Apply the 24-hour diagnostic retention to those copies.

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

Start both services and check that they restart on boot. Connect both work machines with `agent-journal connect --url https://journal.example.com` and [set up Codex](SETUP.md#set-up-codex). Ask Codex to register a repository on one machine, then open Codex in that repository on both machines. Verify that `agent-journal projects` lists the same registration on both machines and that the browser accepts the token over HTTPS.

## Observers

The Codex session hooks start the collector as the same user that runs Codex. It needs access to the shared Codex App Server socket and the saved journal connection.

For a diagnostic or custom deployment without hooks, run `agent-journal observe` in the foreground or through your user service manager. Give a manually managed collector the installed Codex and Agent Journal executables on its PATH.

## Backups and restore

Create the backup on a private filesystem with sufficient disk space. The snapshot command can run while the journal is serving.

For a managed installation, use `/opt/agent-journal/current/data/journal.sqlite3` as the database path and `/opt/agent-journal/current/package/bin/agent-journal` as the executable.

```sh
agent-journal backup --database /var/lib/agent-journal/journal.sqlite3 --output /path/to/private/backup.sqlite3
```

Copy the backup to protected storage. It contains transcripts and notes. To verify it, start a separate test instance against a private copy at a new database path. Check that known turns and notes load. To restore for normal use, point the stopped application service at the verified copy or place it at a new persistent database path and restart the service.

If the access token changes, update the server environment file and restart the service. Run `agent-journal connect` again on each work machine. Close any open Codex sessions to let the collector exit, then reopen Codex. Sign in again in the browser.
