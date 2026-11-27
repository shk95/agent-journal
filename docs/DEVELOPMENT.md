# Agent Journal development

Run these commands from a checkout. For client installation, see the [setup guide](SETUP.md). For a deployed server, see [server operations](OPERATIONS.md).

## Interactive fixture demo

Install `just` and `uv`. Run `just` from the checkout to list the available recipes, then choose a demo.

```sh
just demo-tui
just demo-web
```

Each demo starts the server from the current checkout on a free loopback port and loads synthetic turns and notes into a temporary database. The TUI and web demos open the same fixture project. The web demo opens the fixture directly in your browser. You can also open the URL printed in the terminal. Press `q` in the TUI or `Ctrl+C` in the terminal to stop the server and discard demo data.

`just demo-tui` restores the terminal settings when the demo process exits. If the entire process group is force-killed and the terminal cannot display typed commands correctly, run `stty sane; reset` to recover the pane.

The fixture includes turns with zero, one, and several attached notes, and enough turns and project notes to try both pagination directions. In the TUI, press `1` or `2` to inspect an axis on its own, then toggle the other axis to see the shared turn cards. Use `Tab` to change the active axis and `d/u` to scroll within a page.

## Persistent development server

Run the development server on loopback port 8766.

```sh
uv run --locked python scripts/dev.py serve
```

In a second pane, use `uv run --locked python scripts/dev.py seed` to add the fixture project. Open it with `uv run --locked python scripts/dev.py client tui --project example/journal-fixture`, or run `uv run --locked python scripts/dev.py client web` for the web journal. Get its login token with `uv run --locked python scripts/dev.py token`. The development database and token live under the ignored `.local/dev` directory. To use a separate directory, pass the same `--state PATH` to every development command.

To try registration against the development server, start Codex from the Agent Journal checkout with a session-scoped MCP entry for the development server.

```sh
codex -c 'mcp_servers.journal_dev.command="uv"' \
  -c 'mcp_servers.journal_dev.args=["run","--locked","python","scripts/dev.py","client","mcp"]'
```

Ask Codex to register an accessible GitHub repository using `journal_dev`. Start the development observer in a separate pane.

```sh
uv run --locked python scripts/dev.py client observe
```

Run Codex from that repository as usual. In another pane, open the journal with `uv run --locked python scripts/dev.py client tui --project OWNER/REPO`. The observer uses the local Codex App Server and the signed-in CLI to generate turn explanations.

## Automated checks

Run the software tests locally before publishing. GitHub Actions checks the public repository and builds and smoke-checks the Nix packages when preparing a release.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
node --check src/agent_journal/static/notes.js
node --check src/agent_journal/static/sw.js
node --check src/agent_journal/static/theme.js
node --check src/agent_journal/static/vi.js
uv build
```

## Local Codex and client smoke test

Run the entire interactive path on one development machine with Git, tmux, and Codex CLI 0.172.0 or a compatible version. The test starts a fresh journal server and a separate real Codex App Server, verifies hook trust, and registers the checkout through MCP using a synthetic GitHub CLI response. It checks collection, turn explanations and token counts, local quality changes in the TUI, browser sign-in, expandable turn details, and synchronization of notes. A loopback model fixture supplies responses to both Codex processes.

```sh
uv run --locked --with playwright==1.65.2 python -m playwright install chromium
uv run --locked --with playwright==1.65.2 python -m tests.smoke_local
uv run --locked --with playwright==1.65.2 python -m tests.client_performance
```

If Chrome is installed, omit the browser installation command and pass `--browser-channel chrome` to either browser test. Use `--codex PATH` with the smoke test to select a compatible Codex binary. The performance fixture measures key response during an injected delay, note preview payload size, browser rendering after a slow response, and formatting the first viewport of a long transcript.

See the [test cleanup policy](#test-cleanup-policy) for stopping runs and finding failure reports.

To exercise the standalone client through the same flow, build the bundle and pass its executable path. On Linux, use the release build container to target the same glibc baseline as the published assets. On macOS, if GNU `arch` comes first on your PATH, prefix the build command with `PATH="/usr/bin:$PATH"` so PyInstaller can use the macOS `arch` command.

```sh
uv run --locked --managed-python --python 3.13 --group bundle python scripts/build_client.py
uv run --locked --with playwright==1.65.2 python -m tests.smoke_local \
  --client dist/agent-journal-macos-arm64 --browser-channel chrome
```

## Release validation checklist

Use these scenarios to check workflows changed by a release. Use synthetic content and test projects.

- **Registration and collection.** Register a repository once and record from two worktrees. Check the turns immediately before and after registration, after disabling collection, and after re-enabling it. Use a separate unregistered repository to check the boundary. Confirm that every eligible turn appears once and that existing history remains readable while collection is disabled.
- **Session continuity and timing.** Start turns on two machines and finish the later request first. Fork one session. Compact, resume, and clear the other. Check request ordering, session lines, and execution status in both clients. Measure how long requests and terminal states take to appear. Measure explanation generation separately from collection.
- **Transcripts and notes.** Open a turn's explanation and transcript. Create a turn note in the TUI and edit it in the browser. Create a project note in the browser and read it in the TUI. Ask Codex to read Journey and attach a note to an earlier result. Verify the reported scope, recorded instruction, and note target.
- **Outage and recovery.** Record a turn while the test server is unavailable and restart the observer. Reconnect and check for one copy of the turn and transcript. Check that observer disconnection and turn interruption appear as distinct states. Verify authenticated HTTPS on the test deployment and restore its turns and notes from a backup onto a separate test instance. See [Backups and restore](OPERATIONS.md#backups-and-restore).

## Local deployment smoke test

Run the installer and rollback test on a **fresh, disposable Linux VM with systemd**. A macOS development machine can run it in a local Linux VM. Use a clean checkout of the revision under test, with Nix and Python 3.11 or newer installed inside that VM.

```sh
nix build .#server
sudo python3 scripts/server_deploy.py install --local
systemctl is-active agent-journal.service agent-journal-recover.service
sudo python3 /opt/agent-journal/server_deploy.py auto-update disable
sudo python3 -m tests.test_server_deploy smoke
```

This exercises Linux service startup, SQLite snapshot transfer to the candidate, request fencing, and automatic rollback. The smoke test starts from an empty journal because it installs a synthetic release and deliberately triggers a health failure.

Continue on the same disposable VM to test failed migration, failed startup, and retrying an interrupted database cleanup. Stage each boot case, reboot only the test VM, and run its matching verification command after the VM returns.

```sh
sudo python3 -m tests.test_server_deploy smoke-failures
sudo python3 -m tests.test_server_deploy stage-boot-uncommitted
# Reboot the disposable VM here.
sudo python3 -m tests.test_server_deploy verify-boot-uncommitted
sudo python3 -m tests.test_server_deploy stage-boot-committed
# Reboot the disposable VM here.
sudo python3 -m tests.test_server_deploy verify-boot-committed
sudo python3 -m tests.test_server_deploy smoke-readiness
```

After the final check, retire the disposable VM and its test disk. Follow the [test cleanup policy](#test-cleanup-policy) if you need to collect diagnostics first.

## Test cleanup policy

Local smoke, performance, and compatibility runs use private temporary workspaces. The TUI tests first attempt a normal exit, and the smoke test stops detached collectors from its private state directory. After their processes have stopped, the runners delete the workspace on success, failure, or cancellation. On failure they save the traceback and, when available, the last 64 KiB of the isolated collector log in `.local/test-failures/`. The report directory is private. The retention policy covers these reports. Fixture databases, connection settings, browser profiles, and interactive demo data stay outside the report directory.

Reports expire after 24 hours. The next local integration run removes expired reports, or an operator can run `just clean-test-reports` from the checkout. Collect any diagnostics needed for a longer investigation in private storage before expiry. Review reports for local paths and credentials before sharing. Confirm the disposition of interactive demo data separately, since an interrupted demo may contain notes written by its user.

On a workstation, stop tests with `Ctrl+C` or `SIGTERM` and let the runner finish cleanup. If the runner itself must be force-killed, the supervising job owns cleanup. Use a disposable VM or a supervisor that can stop the entire job scope, including descendants that started their own sessions. For a run already interrupted on a shared workstation, verify the run's private directory and its process IDs or socket before stopping residual processes.

Run the deployment scenarios on [one disposable Linux VM](#local-deployment-smoke-test). Keep its services and disk through the rollback and boot-recovery checks. Retire the VM and its test disk after the last check or when abandoning the run. If a failure needs investigation, keep the VM isolated until the relevant diagnostics are copied, then retire it. Apply the 24-hour diagnostic retention to those copies.

See [the repository instructions](../AGENTS.md) for public file, commit, and privacy rules.
