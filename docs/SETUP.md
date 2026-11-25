# Agent Journal setup and development

## Install

### Nix client

The client flake supports Apple Silicon macOS (`aarch64-darwin`), Linux x86-64 (`x86_64-linux`), and Linux ARM64 (`aarch64-linux`). Enable flakes on the work machine before installing.

```sh
nix profile add 'github:shk95/agent-journal#client'
agent-journal connect --url https://journal.example.com
agent-journal
```

Older Nix versions use `nix profile install`. To run the client directly, use `nix run 'github:shk95/agent-journal#client'`. From a local checkout, use `nix run .#client` or `nix flake check` to verify the package.

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

Connect each work machine once. The command prompts for the token and stores it in the machine's private journal directory.

```sh
agent-journal connect --url https://journal.example.com
```

For service managers and temporary tests, the `AGENT_JOURNAL_TOKEN` and `AGENT_JOURNAL_URL` environment variables override saved settings. For single-machine development, you can use the default loopback address.

Register a repository from its checkout.

```sh
agent-journal register
```

Start one observer per work machine, then run Codex as usual from the repository.

```sh
agent-journal observe
```

Open the terminal client from a second pane in the same repository. It reads the GitHub `origin` from the current checkout, including from a subdirectory. To select a different project, run `agent-journal tui --project OWNER/REPO`.

```sh
agent-journal
```

- Toggle Journey, Turn notes, and Project notes with `1`, `2`, and `3`.
- Select a turn with `j/k`, open its transcript with `Enter`, and return with `Esc`.
- Use `m` for a turn note and `n` for a project note.
- Use `e` to select a note and open it in `VISUAL`, `EDITOR`, or `vi`. Use `b` for older turns and `g` for the latest page.
- Click a turn to select it, a note to edit it, or a bracketed control to run its action. Scroll over the Journey, Project notes, or an open transcript with the wheel where the terminal and curses support it.

Mouse input is enabled by default. In tmux, enable forwarding with `set -g mouse on`. Use `agent-journal tui --no-mouse` to leave mouse selection to the terminal or tmux.

Run `agent-journal web` to open the overview in your browser, or `agent-journal web --project OWNER/REPO` to open a project directly. The command prints the URL for use in another browser or on a remote terminal. Sign in to the web journal with the server access token.

Use `agent-journal projects` to see the registration state. Use `agent-journal disable` to stop collecting turns for the current project. You can still read the recorded history.

## Collection and summaries

The observer reads sessions and turns every three seconds by default. Clones and worktrees with the same GitHub `owner/repo` share a journal. The observer recovers subsecond start times from Codex turn IDs when available. Older records with second-only timestamps have a conservative cutoff. Turning collection back on sets a fresh cutoff.

The observer sends transcripts and a preliminary summary first. A separate worker runs a read-only, ephemeral `codex exec` in a temporary directory to generate an English summary. This uses model capacity. Set `AGENT_JOURNAL_SUMMARY_MODEL` to select a summary model. The preliminary summary remains visible while a failed generation waits for retry.

During a server outage, the observer uses its cached list of registered projects and holds the latest unacknowledged snapshots in SQLite. It clears pending payloads after successful delivery. The clients show when the observer loses contact with a previously running turn or recording stops for the project. The local state directory defaults to `~/.agent-journal` and can be set with `AGENT_JOURNAL_HOME`.

Collection uses the shared Codex App Server. Use `observe --socket PATH` for a different server socket. The [release plan](../RELEASE_PLAN.md) tracks selective import of older sessions.

## Read or write from Codex

Register the local MCP interface once on a connected work machine. Restart Codex after changing the configuration.

```sh
codex mcp add journal -- agent-journal mcp
```

Provide the GitHub project when Codex cannot infer it from the working directory. Ask Codex to name the axes it reads. For example, ask it to read Journey and then add a note to the previous result from this session. The tool locally resolves the current request and returns the preceding turns with their IDs. If two concurrent requests have identical text, specify the target session and turn.

See [cloud operations](OPERATIONS.md) for deployment and backup restoration.

## Local development

### Interactive fixture demo

Install `just` and `uv`, then run either command from the checkout.

```sh
just demo
just demo web
```

Each command starts the server from the current checkout on a free loopback port and loads synthetic turns and notes into a temporary database. The default opens the terminal client on the fixture project. The web demo opens the same fixture in your browser. Use the temporary token printed in the terminal to sign in. Press `q` in the TUI or `Ctrl+C` in the terminal to stop the server and discard demo data. Each demo uses its own connection settings and browser sign-in cookie.

### Persistent development server

Run the development server on loopback port 8766.

```sh
uv run --locked python scripts/dev.py serve
```

In a second pane, use `uv run --locked python scripts/dev.py seed` to add the fixture project. Open it with `uv run --locked python scripts/dev.py client tui --project example/journal-fixture`, or run `uv run --locked python scripts/dev.py client web` for the web journal. Get its login token with `uv run --locked python scripts/dev.py token`. The development database and token live under the ignored `.local/dev` directory. To use a separate directory, pass the same `--state PATH` to every development command.

To try your normal Codex workflow against the development server, register a GitHub repository and start the observer in a separate pane.

```sh
uv run --locked python scripts/dev.py client register --project OWNER/REPO
uv run --locked python scripts/dev.py client observe
```

Run Codex from that repository as usual. In another pane, open the journal with `uv run --locked python scripts/dev.py client tui --project OWNER/REPO`. The observer uses the local Codex App Server and the signed-in CLI for real summaries.

### Automated checks

Run the software tests locally before publishing. GitHub Actions checks the public repository and builds and smoke-checks the Nix packages when preparing a release.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
uv build
```

### Local Codex and client smoke test

Run the entire interactive path on one development machine with Git, tmux, and Codex CLI 0.172.0 or a compatible version. The test starts a fresh journal server, a separate real Codex App Server, the summary worker and the observer. It checks a running turn and its final summary, a Codex MCP note on the previous turn, and synchronization of notes between the TUI and browser. It also opens the TUI from a nested Git directory and exercises keyboard and mouse input. A loopback model fixture supplies responses to both Codex processes. The test uses temporary databases, a Git checkout, sockets, and a generated journal token.

```sh
uv run --locked --with playwright==1.65.2 python -m playwright install chromium
uv run --locked --with playwright==1.65.2 python -m tests.smoke_local
```

If Chrome is installed, omit the browser installation command and pass `--browser-channel chrome` to the smoke test. Use `--codex PATH` to select a compatible Codex binary.

### Local deployment smoke test

Run the installer and rollback test on a **fresh, disposable Linux VM with systemd**. A macOS development machine can run it in a local Linux VM. Use a clean checkout of the revision under test, with Nix and Python 3.11 or newer installed inside that VM.

```sh
nix build .#server
sudo python3 scripts/server_deploy.py install
systemctl is-active agent-journal-update.timer
sudo systemctl stop agent-journal-update.timer agent-journal-update.service
sudo python3 -m tests.test_server_deploy smoke
```

This exercises Linux service startup, SQLite snapshot transfer to the candidate, request fencing, and automatic rollback. The smoke test starts from an empty journal because it installs a synthetic release and deliberately triggers a health failure.

See [the repository instructions](../AGENTS.md) for public file, commit, and privacy rules.
