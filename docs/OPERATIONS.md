# Cloud operations

Run the central server on a Linux VM with a persistent volume and an HTTPS reverse proxy. The examples use systemd and Caddy. Install Agent Journal from a reviewed Git revision before creating the service.

## Server

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

```sh
agent-journal backup --database /var/lib/agent-journal/journal.sqlite3 --output /path/to/private/backup.sqlite3
```

Copy the backup to protected storage. It contains transcripts and notes. To verify it, start a separate test instance against a private copy at a new database path. Check that known turns and notes load. To restore for normal use, point the stopped application service at the verified copy or place it at a new persistent database path and restart the service.

If the access token changes, update the server environment file and restart the service. Run `agent-journal connect` again on each work machine, then restart its observer. Sign in again in the browser.
