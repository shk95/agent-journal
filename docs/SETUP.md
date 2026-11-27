# Set up Agent Journal

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

The installer selects the matching release asset, verifies its SHA-256 checksum and version, and installs `agent-journal` in `~/.local/bin`. Add that directory to your PATH to launch the TUI by name. Set `AGENT_JOURNAL_BIN_DIR` to install somewhere else. Pass a release tag such as `v0.8.0` to the installer to select a specific version. Run the installer again to upgrade.

Verify the installed client with `agent-journal --version`. If Codex is on PATH, the installer installs the user-level `journal` MCP entry and recording hooks. Complete [the first server connection](#connect-a-work-machine), then [trust the hooks once in Codex](#set-up-codex).

### Optional Nix client

The client flake supports Apple Silicon macOS (`aarch64-darwin`), Linux x86-64 (`x86_64-linux`), and Linux ARM64 (`aarch64-linux`). Enable flakes on the work machine before installing.

```sh
nix profile add 'github:shk95/agent-journal/v0.8.0#client'
```

Older Nix versions use `nix profile install`. Substitute the desired release tag to pin a different version. To run the client directly, use `nix run 'github:shk95/agent-journal/v0.8.0#client'`. From a local checkout, use `nix run .#client` or `nix flake check` to verify the package.

The flake reads Python dependencies from `uv.lock` and includes Git and the GitHub CLI. Observation and turn explanations use the signed-in Codex CLI on your PATH. Sign in to GitHub CLI before registering a repository.

### Python package

Install Python 3.11 or newer and uv. Run the following command from the project checkout on a work machine or on the server.

```sh
uv tool install --editable .
```

Work machines need Git, a signed-in Codex CLI, and GitHub CLI credentials that can read any repository you register. The local App Server integration was verified with Codex CLI 0.172.0.

## Run the central server

Use an existing Agent Journal server or follow the [managed installation](OPERATIONS.md#managed-installation). To start a server directly from a shell, see [Run a basic server](OPERATIONS.md#run-a-basic-server).

## Connect a work machine

Set up the server connection once per work machine, before registering or using a journal project. From a GitHub checkout, run `agent-journal` to open the terminal client, or run `agent-journal web` to open the web journal. On the first interactive run, the client prompts for the server URL and access token. It verifies the connection and saves the settings in `~/.agent-journal/connection.json`. You can also run the setup explicitly from any directory.

```sh
agent-journal connect
```

If you start from Codex, the first journal tool call prompts you to run this command when setup is required. Enter the server URL and access token in your local terminal, then retry the request in Codex.

The TUI, local MCP tools, and observer use the same saved connection on each work machine. See [connection settings](CONFIGURATION.md#server-connection) for changing the saved connection or using environment overrides.

## Set up Codex

The standalone installer configures Codex globally when the CLI is available. For an existing installation or a client installed another way, run the same setup once per work machine.

```sh
agent-journal setup-codex
```

Open Codex and use `/hooks` to review and trust the Agent Journal `SessionStart` and `SessionEnd` hooks. Then start a new session. Trust applies to the hook definitions and is reused across repositories and regular client upgrades at the same installation path. Recording uses Codex CLI 0.172.0 or a compatible version with the shared App Server.

If you are upgrading from a release that required a manually started collector, stop that process before opening the first Codex session with hooks.

After connecting the machine, ask Codex to register the repository from its checkout. Its `register_project` tool verifies the journal connection, checks the repository using the signed-in GitHub CLI, and marks it active on the journal server. Subsequent turns in that repository are recorded automatically. If the checkout is ambiguous, give Codex the GitHub `OWNER/REPO`. You can ask Codex to register the same project again to resume collection after disabling it.

Ask Codex to name the axes it reads. For example, ask it to read Journey and then add a note to the previous result from this session. The tool locally resolves the current request and returns the preceding turns with their IDs. If two concurrent requests have identical text, specify the target session and turn.

## Use the journal

Open the terminal client from a second pane in the same repository. It reads the GitHub `origin` from the current checkout, including from a subdirectory, and exits with a message if it cannot identify a registered project. To select a different project, run `agent-journal tui --project OWNER/REPO`.

```sh
agent-journal
```

- Press `1`, `2`, or `3` to open Journey, Turn notes, or Project notes on its own. Inside that view, use the same keys to show or hide axes. At least one axis stays open.
- Use `h/l` to move to the visible axis on the left or right, or `Tab` and `Shift+Tab` to cycle through the axes. Use `j/k` to move one item or line and `d/u` to move half a screen. Use `gg/G` for the first or last item on the current page. `Older` (`[`) and `Newer` (`]`) move one page. `Oldest` (`<`) and `Latest` (`>`) jump to the ends of the history for the focused axis.
- Scroll over an axis with the mouse wheel or focus it before using the keyboard. Journey and Turn notes share a scrolling pane when both are visible. Each card contains the turn and its attached notes. Project notes scroll independently.
- Press `Enter` to open a transcript or note. Parent layers remain visible at the top of the screen. `Esc` closes the top layer and restores the parent selection and scroll position. On short terminals, the path above the content identifies the active layer.
- A Journey turn opens the turn explanation above its transcript. Each box scrolls independently and has a **View details** control that opens its full content in a separate dialog. In the terminal, press `1` for the explanation or `2` for the transcript. Use `Tab` to switch boxes and `Enter` to expand the selected box. In the browser, focus the box before using the scrolling keys, or scroll over it directly. Close the expanded dialog to return to the same position in the turn details.
- Turn notes includes turns without notes. Use `m` to add a note to the selected turn, `n` for a project note, and `e` to edit an existing note in place. In either editor, `i` enters Insert mode and `Ctrl+P` toggles the Markdown preview. To return to Normal mode or cancel a pending command, press `Ctrl+;` in the browser or `Esc` in the TUI. Press `Enter` after `:w` to queue an immediate save, `:q` to keep pending edits and close, or `:wq` to save and close. The display wraps long lines without changing the stored Markdown. In the editor, use the up and down arrow keys to move through wrapped rows.
- In a note detail, `D` prompts for deletion. In a turn note, `x` selects it for moving. Navigate to another turn and press `p` to move it, or `X` to cancel the cut.
- Use `Status` in a note detail in either client to choose Highlighted, Normal, or Low priority. In the TUI, press `s` to open the menu and `1`, `2`, or `3` to choose a status. Existing and new notes default to Normal. The status changes the title's appearance and preserves the note's position. Status changes require a server connection.
- Click a turn to select it, a note to open it, or a bracketed control to run its action. Shortcuts appear beside the controls where space permits. For example, `[Edit] e` opens the editor when you press `e` or click the control. Use the wheel over a column or inside an open item where the terminal and curses support it.

Notes use Markdown. Write a heading such as `# Import results` to name the note. The first top-level heading supplies the title, and a leading title heading appears once in the detail view. Notes without a heading appear as `Untitled note`. Note details and previews show single line breaks and spacing between paragraphs.

When a turn informs a decision, record your reasoning in a turn note. For example, note why you kept the current import format in the turn where you reviewed it. Use a project note to keep a follow-up across sessions, such as testing blank records. When you resume, ask Codex to read the relevant Journey history and Project notes, then choose what to verify or do next.

Long lines continue at the display edge. The `↳` gutter marks a continuation of the same source line. In Normal mode, `w/b/e` move by word, `W/B/E` move by whitespace-delimited word, and `^` moves to the first nonblank character. `I` inserts at the first nonblank character, and `A` appends at the end of the source line. `D` deletes to the end of the line, `C` changes to the end of the line, `s` substitutes a character, and `X` deletes the preceding character.

Use `:help` in either editor for the full key list. `gj/gk` move through wrapped display rows, while `j/k` and `A/$` use source lines. Counts work with moves and operators, such as `3w`, `2dd`, and `d2w`. Operators also work with text objects, such as `ciw` and `ci"`. Use `v`, `V`, or `Ctrl+V` to select characters, lines, or a block. Block columns follow text characters, with grapheme clusters kept together.

Use `/` and `?` to search with a regular expression, then `n/N` to repeat. Enter `:s/pattern/replacement/` for the current line or `:%s/pattern/replacement/g` for all matches in the note. The shared regular expression syntax uses `(…)` for capture groups and `\1` for a captured group in the replacement. Use `u` to undo an edit, `Ctrl+R` to redo, and `.` to repeat the last edit. One Insert or Replace session is one undo step. Marks, registers, and recorded macros are available for the current editor session.

The Journey graph connects each session's turns in a continuous column beside the turn cards. Each turn has a short session label in its graph color. Focusing a turn shades every card from the same session, including its attached notes. The TUI underlines the labels in terminals with fewer than 256 colors or when `NO_COLOR=1` is set.

In Journey and Turn notes, use `Shift+↑` or `K` for the previous turn in the current session and `Shift+↓` or `J` for the next. This follows one session across pages, including turns with no notes. In Turn notes, the shortcut selects the first note on the destination turn or the empty turn's note entry.

Mouse input is enabled by default. In tmux, enable forwarding with `set -g mouse on`. Use `agent-journal tui --no-mouse` to leave mouse selection to the terminal or tmux.

Press `w` in the TUI to open the current project in your browser. From the command line, run `agent-journal web` for the overview or `agent-journal web --project OWNER/REPO` for a project. The client uses its existing connection to sign the browser in without prompting for the token. The same browser can then reopen the journal from a bookmark after a restart. Its sign-in lasts up to one year and is invalidated when the server token changes. In a browser without a session, open the journal from the client or enter the server token in the sign-in form. The project board supports the same `h/l`, `j/k`, `gg/G`, `[/]`, and `</>` navigation as the TUI. In Turn notes, keyboard navigation focuses `+ Note` on turns without notes.

In the browser overview, use **New note project** or **New note group** to create a space for Markdown notes. A group has its own notes and can contain projects, each with a separate set of notes. These spaces use the same editor, status choices, and synchronization as repository project notes. Their pages show only Project notes and any projects in the group.

Open `Session filter` above Journey and pick a session. `Highlight` keeps its turns highlighted as you move around the board. `Show only` limits Journey and its attached Turn notes to that session across all pages. `Clear filter` returns to all sessions.

Use the theme icons in the Web header to switch appearance. See [browser preferences](CONFIGURATION.md#browser-preferences) for theme and time zone defaults and reset options.

Both clients retain a recent view cache and save note drafts locally. After about one second without input, the editor sends the latest draft to the server. `Saved` indicates acknowledgement from the server. `Saved locally` means the draft is available on the device while synchronization is pending. Use `:q!` to discard the local draft and close the editor. Any save request already sent to the server may still finish. On a version conflict, enter `:review` to review the server version. The Web editor also provides a review button. Use a UTF-8 locale in the terminal for multilingual input and display.

Use `agent-journal projects` to see the registration state. Use `agent-journal disable` to stop collecting turns for the current project. You can still read the recorded history.

Run `agent-journal server-version` to print the connected server's version. Older servers may report their release through the deployment ID. If they provide neither version nor release ID, the command prints `unknown`. Use `agent-journal --version` for the local client.

Run `agent-journal stats` or open **Storage** in the web journal to see the active database and WAL file sizes, the data in turns started during the last 30 days, and counts and sizes by project. In the web journal, select a project in the storage table to see its ten largest turns and open their transcripts.

Project sizes count transcript JSON, stored prompts and summaries, and note text in bytes. The file sizes also include SQLite overhead and reusable pages. They exclude deployment snapshots and separate backups.

### Display time zone

Run `agent-journal timezone` or look in the client header to see the active time zone. See [display time zone](CONFIGURATION.md#display-time-zone) for changing it and how it applies when opening the Web journal from the client.

## Collection and turn explanations

The observer reads sessions and turns every three seconds by default. Clones and worktrees with the same GitHub `owner/repo` share a journal. The observer recovers subsecond start times from Codex turn IDs when available. Older records with second-only timestamps have a conservative cutoff.

The observer sends transcripts and a preliminary line first. A separate worker runs a read-only, ephemeral `codex exec` in a temporary directory to generate a Journey line and a standalone explanation. Both follow the language explicitly requested for the current answer or the main language of the current request. A language-neutral follow-up uses the preceding request's language when evident, with English as the default. The explanation names the task, connects relevant context to what happened, and explains the outcome. In-depth expands complex turns with concrete decisions, verification results, and remaining work when the transcript supports them. Older turns and turns awaiting an explanation display their available brief line.

### Explanation settings

Run `agent-journal explanation` to check the current quality. See [explanation settings](CONFIGURATION.md#explanation-settings) for quality levels, model selection, and token measurement.

### Collection lifecycle

During a server outage, the observer uses its cached list of registered projects and holds the latest unacknowledged snapshots in SQLite. It clears pending payloads after successful delivery. The clients show when the observer loses contact with a previously running turn or recording stops for the project.

Collection uses the shared Codex App Server. Trusted hooks start one collector when a session opens or resumes. While running, it also reads already-open sessions visible to that App Server. Collection starts at the project's current registration cutoff, including when collection is re-enabled. The collector finishes pending work after the last tracked session ends and resumes its local queue the next time it starts. For troubleshooting or a custom App Server socket, use `agent-journal observe --socket PATH` to run it in the foreground. Diagnostics go to `observer.log` in the [local state directory](CONFIGURATION.md#local-state-directory).

## Local development

See the [development guide](DEVELOPMENT.md) for fixture demos, local checks, release validation, and test cleanup.
