# Agent Journal setup and development

## Install

### Nix client

Install the client CLI on a work machine with flakes enabled.

```sh
nix profile add 'github:shk95/agent-journal#client'
agent-journal connect --url https://journal.example.com
agent-journal tui
```

Older Nix versions use `nix profile install`. To run the client directly, use `nix run 'github:shk95/agent-journal#client' -- tui`. From a local checkout, use `nix run .#client -- tui` or `nix flake check` to verify the package.

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

Open the terminal client from a second pane in the same repository.

```sh
agent-journal tui
```

- Toggle Journey, Turn notes, and Project notes with `1`, `2`, and `3`.
- Select a turn with `j/k`, open its transcript with `Enter`, and return with `Esc`.
- Use `m` for a turn note and `n` for a project note.
- Use `e` to select a note and open it in `VISUAL`, `EDITOR`, or `vi`. Use `b` for older turns and `g` for the latest page.
- Open the server address in a browser and sign in with the token. Select a project to read its journal and select a turn to read its transcript.

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

Run the development server on loopback port 8766.

```sh
uv run --locked python scripts/dev.py serve
```

From a second pane, seed synthetic data and open the terminal client.

```sh
uv run --locked python scripts/dev.py seed
uv run --locked python scripts/dev.py client tui --project example/journal-fixture
```

Open `http://127.0.0.1:8766` in a browser. Get its login token with `uv run --locked python scripts/dev.py token`. The development database and token live under the ignored `.local/dev` directory. To use a separate directory, pass the same `--state PATH` to every development command. To observe a real Codex session, use the development client's `register --project OWNER/REPO` and `observe` commands.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
uv build
```

See [the repository instructions](../AGENTS.md) for public file, commit, and privacy rules.
