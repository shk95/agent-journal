# Agent Journal setup and development

## Install

### Standalone client

Use the standalone GitHub Release installer by default, including when an agent is asked to install the client using the repository URL. It needs no Nix, Python installation, or source checkout. The client supports Apple Silicon macOS, Linux x86-64, and Linux ARM64. The Linux builds require glibc 2.35 or later.

```sh
(
  set -eu
  installer=$(mktemp)
  trap 'unlink "$installer"' EXIT
  curl -fsSL --proto '=https' \
    https://github.com/shk95/agent-journal/releases/latest/download/install-client.sh \
    -o "$installer"
  sh "$installer"
)
```

The installer selects the matching release asset, verifies its SHA-256 checksum and version, and installs `agent-journal` in `~/.local/bin`. Add that directory to your PATH to launch the TUI by name. Set `AGENT_JOURNAL_BIN_DIR` to install somewhere else. Pass a release tag such as `v0.4.0` to the installer to select a specific version. Run the installer again to upgrade.

Verify the installed client with `agent-journal --version`. If Codex is on PATH, the installer installs the user-level `journal` MCP entry and recording hooks. Complete [the first server connection](#connect-a-work-machine), then [trust the hooks once in Codex](#set-up-codex).

### Optional Nix client

The client flake supports Apple Silicon macOS (`aarch64-darwin`), Linux x86-64 (`x86_64-linux`), and Linux ARM64 (`aarch64-linux`). Enable flakes on the work machine before installing.

```sh
nix profile add 'github:shk95/agent-journal/v0.4.0#client'
```

Older Nix versions use `nix profile install`. Substitute the desired release tag to pin a different version. To run the client directly, use `nix run 'github:shk95/agent-journal/v0.4.0#client'`. From a local checkout, use `nix run .#client` or `nix flake check` to verify the package.

The flake reads Python dependencies from `uv.lock` and includes Git and the GitHub CLI. Observation and summaries use the signed-in Codex CLI on your PATH. Sign in to GitHub CLI before registering a repository.

### Python package

Install Python 3.11 or newer and uv. Run the following command from the project checkout on a work machine or on the server.

```sh
uv tool install --editable .
```

Work machines need Git, a signed-in Codex CLI, and GitHub CLI credentials that can read any repository you register. The local App Server integration was verified with Codex CLI 0.172.0.

## Run the central server

Generate an access token once and use the same token on the server and work machines.

```sh
export AGENT_JOURNAL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
agent-journal serve
```

The default address is `http://127.0.0.1:8765`. For cloud use, keep the database on a persistent volume and connect through an HTTPS reverse proxy. Use `serve --database /path/to/journal.sqlite3` to set the database location. Summaries are produced on the work machines.

## Connect a work machine

Set up the server connection once per work machine, before registering or using a journal project. From a GitHub checkout, run `agent-journal` to open the terminal client, or run `agent-journal web` to open the web journal. On the first interactive run, the client prompts for the server URL and access token. It verifies the connection and saves the settings in `~/.agent-journal/connection.json`. You can also run the setup explicitly from any directory.

```sh
agent-journal connect
```

If you start from Codex, the first journal tool call prompts you to run this command when setup is required. Enter the server URL and access token in your local terminal, then retry the request in Codex.

The TUI, local MCP tools, and observer use the same saved connection on each work machine. The `AGENT_JOURNAL_HOME` environment variable selects a different state directory. For service managers and temporary tests, `AGENT_JOURNAL_TOKEN` and `AGENT_JOURNAL_URL` override saved settings.

If you installed with Nix or from source, run [Codex setup](#set-up-codex). Ask Codex to register the repository with Agent Journal. With the recording hooks trusted, Codex starts collection automatically.

Open the terminal client from a second pane in the same repository. It reads the GitHub `origin` from the current checkout, including from a subdirectory, and exits with a message if it cannot identify a registered project. To select a different project, run `agent-journal tui --project OWNER/REPO`.

```sh
agent-journal
```

- Press `1`, `2`, or `3` to open Journey, Turn notes, or Project notes on its own. Inside that view, use the same keys to show or hide axes. At least one axis stays open.
- Use `h/l` to move to the visible axis on the left or right, or `Tab` and `Shift+Tab` to cycle through the axes. Use `j/k` to move one item or line and `d/u` to move half a screen. Use `gg/G` for the first or last item on the current page. `Older` (`[`) and `Newer` (`]`) move one page. `Oldest` (`<`) and `Latest` (`>`) jump to the ends of the history for the focused axis.
- Scroll over an axis with the mouse wheel or focus it before using the keyboard. Journey and Turn notes share a scrolling pane when both are visible. Each card contains the turn and its attached notes. Project notes scroll independently.
- Press `Enter` to open a transcript or note. Parent layers remain visible at the top of the screen. `Esc` closes the top layer and restores the parent selection and scroll position. On short terminals, the path above the content identifies the active layer.
- Use `m` for a turn note, `n` for a project note, and `e` to edit an existing note in place. In the editor, `i` enters Insert mode, `Esc` returns to Normal mode, `Ctrl+P` toggles the Markdown preview, and `:w` queues an immediate save.
- In a note detail, `D` prompts for deletion. In a turn note, `x` selects it for moving. Navigate to another turn and press `p` to move it, or `X` to cancel the cut.
- Use `Status` in a note detail in either client to choose Highlighted, Normal, or Low priority. In the TUI, press `s` to open the menu and `1`, `2`, or `3` to choose a status. Existing and new notes default to Normal. The status changes the title's appearance and preserves the note's position. Status changes require a server connection.
- Click a turn to select it, a note to open it, or a bracketed control to run its action. Shortcuts appear beside the controls where space permits. For example, `[Edit] e` opens the editor when you press `e` or click the control. Use the wheel over a column or inside an open item where the terminal and curses support it.

Notes use Markdown. Write a heading such as `# Import results` to name the note. The first top-level heading supplies the title, and a leading title heading appears once in the detail view. Notes without a heading appear as `Untitled note`. Note details and previews show single line breaks and spacing between paragraphs.

The Journey graph connects each session's turns in a continuous column beside the turn cards. Each turn has a short session label in its graph color. Focusing a turn shades every card from the same session, including its attached notes. The TUI underlines the labels in terminals with fewer than 256 colors or when `NO_COLOR=1` is set.

Mouse input is enabled by default. In tmux, enable forwarding with `set -g mouse on`. Use `agent-journal tui --no-mouse` to leave mouse selection to the terminal or tmux.

Run `agent-journal web` to open the overview in your browser, or `agent-journal web --project OWNER/REPO` to open a project directly. The command prints the URL for use in another browser or on a remote terminal. Sign in to the web journal with the server access token. The project board supports the same `h/l`, `j/k`, `gg/G`, `[/]`, and `</>` navigation as the TUI.

Open `Session filter` above Journey and pick a session. `Highlight` keeps its turns highlighted as you move around the board. `Show only` limits Journey and its attached Turn notes to that session across all pages. `Clear filter` returns to all sessions.

Both clients retain a recent view cache and save note drafts locally. After about one second without input, the editor sends the latest draft to the server. `Saved` indicates acknowledgement from the server. `Saved locally` means the draft is available on the device while synchronization is pending. On a version conflict, the terminal editor uses `Ctrl+R` to review the server version and the browser provides a review button. Use a UTF-8 locale in the terminal for multilingual input and display.

Use `agent-journal projects` to see the registration state. Use `agent-journal disable` to stop collecting turns for the current project. You can still read the recorded history.

Run `agent-journal server-version` to print the connected server's version. Older servers may report their release through the deployment ID. If they provide neither version nor release ID, the command prints `unknown`. Use `agent-journal --version` for the local client.

Run `agent-journal stats` or open **Storage** in the web journal to see the active database and WAL file sizes, the data in turns started during the last 30 days, and counts and sizes by project. In the web journal, select a project in the storage table to see its ten largest turns and open their transcripts.

Project sizes count transcript JSON, stored prompts and summaries, and note text in bytes. The file sizes also include SQLite overhead and reusable pages. They exclude deployment snapshots and separate backups.

## Collection and summaries

The observer reads sessions and turns every three seconds by default. Clones and worktrees with the same GitHub `owner/repo` share a journal. The observer recovers subsecond start times from Codex turn IDs when available. Older records with second-only timestamps have a conservative cutoff. Turning collection back on sets a fresh cutoff.

The observer sends transcripts and a preliminary summary first. A separate worker runs a read-only, ephemeral `codex exec` in a temporary directory to generate an English summary. This uses model capacity. Set `AGENT_JOURNAL_SUMMARY_MODEL` to select a summary model. The preliminary summary remains visible while a failed generation waits for retry.

During a server outage, the observer uses its cached list of registered projects and holds the latest unacknowledged snapshots in SQLite. It clears pending payloads after successful delivery. The clients show when the observer loses contact with a previously running turn or recording stops for the project. The local state directory defaults to `~/.agent-journal` and can be set with `AGENT_JOURNAL_HOME`.

Collection uses the shared Codex App Server. Session hooks start one collector for concurrent Codex sessions and let it finish pending work after the last session ends. From the next session it resumes any work saved to the local queue. For troubleshooting or a custom App Server socket, use `agent-journal observe --socket PATH` to run the collector in the foreground. Automatic collector diagnostics go to `observer.log` in the local state directory. The [release plan](../RELEASE_PLAN.md) tracks selective import of older sessions.

## Set up Codex

The standalone installer configures Codex globally when the CLI is available. For an existing installation or a client installed another way, run the same setup once per work machine.

```sh
agent-journal setup-codex
```

Open Codex and use `/hooks` to review and trust the Agent Journal `SessionStart` and `SessionEnd` hooks. Then start a new session. Trust applies to the hook definitions and is reused across repositories and regular client upgrades at the same installation path. Recording uses Codex CLI 0.172.0 or a compatible version with the shared App Server.

If you are upgrading from a release that required a manually started collector, stop that process before opening the first Codex session with hooks.

After connecting the machine, ask Codex to register the repository from its checkout. Its `register_project` tool verifies the journal connection, checks the repository using the signed-in GitHub CLI, and marks it active on the journal server. Subsequent turns in that repository are recorded automatically. If the checkout is ambiguous, give Codex the GitHub `OWNER/REPO`. You can ask Codex to register the same project again to resume collection after disabling it.

Ask Codex to name the axes it reads. For example, ask it to read Journey and then add a note to the previous result from this session. The tool locally resolves the current request and returns the preceding turns with their IDs. If two concurrent requests have identical text, specify the target session and turn.

See [cloud operations](OPERATIONS.md) for deployment and backup restoration.

## Local development

### Interactive fixture demo

Install `just` and `uv`. Run `just` from the checkout to list the available recipes, then choose a demo.

```sh
just demo-tui
just demo-web
```

Each demo starts the server from the current checkout on a free loopback port and loads synthetic turns and notes into a temporary database. The TUI and web demos open the same fixture project. To sign in to the web demo, use the temporary token printed in the terminal. Press `q` in the TUI or `Ctrl+C` in the terminal to stop the server and discard demo data. Each demo uses its own connection settings and browser sign-in cookie.

`just demo-tui` restores the terminal settings when the demo process exits. If the entire process group is force-killed and the terminal cannot display typed commands correctly, run `stty sane; reset` to recover the pane.

The fixture includes turns with zero, one, and several attached notes, and enough turns and project notes to try both pagination directions. In the TUI, press `1` or `2` to inspect an axis on its own, then toggle the other axis to see the shared turn cards. Use `Tab` to change the active axis and `d/u` to scroll within a page.

### Persistent development server

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

Run Codex from that repository as usual. In another pane, open the journal with `uv run --locked python scripts/dev.py client tui --project OWNER/REPO`. The observer uses the local Codex App Server and the signed-in CLI for real summaries.

### Automated checks

Run the software tests locally before publishing. GitHub Actions checks the public repository and builds and smoke-checks the Nix packages when preparing a release.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
node --check src/agent_journal/static/notes.js
node --check src/agent_journal/static/sw.js
uv build
```

### Local Codex and client smoke test

Run the entire interactive path on one development machine with Git, tmux, and Codex CLI 0.172.0 or a compatible version. The test starts a fresh journal server and a separate real Codex App Server, verifies that the installed hooks require trust, and trusts them in the temporary Codex configuration. Codex starts and stops the collector as sessions open and close, and registers the checkout through MCP using a synthetic GitHub CLI response. The test checks a running turn and its final summary, a Codex MCP note on the previous turn, and synchronization of notes between the TUI and browser. It opens the TUI from a nested Git directory and exercises keyboard and mouse input. A loopback model fixture supplies responses to both Codex processes.

```sh
uv run --locked --with playwright==1.65.2 python -m playwright install chromium
uv run --locked --with playwright==1.65.2 python -m tests.smoke_local
uv run --locked --with playwright==1.65.2 python -m tests.client_performance
```

If Chrome is installed, omit the browser installation command and pass `--browser-channel chrome` to either browser test. Use `--codex PATH` with the smoke test to select a compatible Codex binary. The performance fixture measures key response during an injected delay, note preview payload size, browser rendering after a slow response, and formatting the first viewport of a long transcript.

The smoke, performance, and compatibility runners clean up their test processes on completion, failure, and termination signals. The TUI tests first attempt a normal exit. The smoke test also stops detached collectors from its private state directory. Stop a local run with `Ctrl+C` or `SIGTERM` to let this cleanup run. See the [test cleanup policy](OPERATIONS.md#test-cleanup-policy) for failure reports, retention, and forced termination.

To exercise the standalone client through the same flow, build the bundle and pass its executable path. On Linux, use the release build container to target the same glibc baseline as the published assets.

```sh
uv run --locked --managed-python --python 3.13 --group bundle python scripts/build_client.py
uv run --locked --with playwright==1.65.2 python -m tests.smoke_local \
  --client dist/agent-journal-macos-arm64 --browser-channel chrome
```

### Local deployment smoke test

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

After the final check, retire the disposable VM and its test disk. Follow the [test cleanup policy](OPERATIONS.md#test-cleanup-policy) if you need to collect diagnostics first.

See [the repository instructions](../AGENTS.md) for public file, commit, and privacy rules.
