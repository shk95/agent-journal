# Agent Journal configuration

Use this reference to change settings after [installing and connecting the client](SETUP.md). The terminal client, local MCP tools, and observer share the settings and connection on each work machine. The Web journal saves display preferences separately for each browser profile and journal origin.

## Local state directory

Agent Journal uses `~/.agent-journal` by default. Set `AGENT_JOURNAL_HOME` to another directory for the processes that should use it. The directory holds `settings.json`, `connection.json`, and local state such as drafts, the observer queue, logs, and the explanation usage database. Use the same directory for processes that should share that state.

## Client settings file

The client reads `settings.json` from the local state directory. Omitted keys use their defaults. CLI commands update individual settings and preserve the others. Use a JSON object when editing the file. This example sets a display time zone and customizes just one explanation level.

```json
{
  "time_zone": "Asia/Seoul",
  "explanation_quality": "Contextual",
  "explanation_levels": {
    "Contextual": "Connect relevant earlier decisions to the outcome."
  },
  "measure_explanation_usage": true
}
```

| Key | Accepted values | Default | Change or reset |
| --- | --- | --- | --- |
| `time_zone` | An IANA time zone such as `Asia/Seoul` or `UTC`, or `null` or `"default"` | `null`, which uses UTC locally | Run `agent-journal timezone ZONE` to set it or `agent-journal timezone default` to restore UTC. |
| `explanation_quality` | `Basic`, `Contextual`, or `In-depth`. See the aliases below. | `Contextual` | Run `agent-journal explanation QUALITY`. To reset, use `agent-journal explanation Contextual`. |
| `explanation_levels` | An object with any of the level names above as keys and nonempty instruction strings as values. Use the English level names as keys. | Built-in instructions for each level | Edit the file. Remove a level entry to restore its built-in instructions. |
| `measure_explanation_usage` | `true` or `false` | `true` | Run `agent-journal explanation --measure-usage off` to stop future recording or `agent-journal explanation --measure-usage on` to resume it. |

### Explanation settings

Run `agent-journal explanation` to see the current quality and whether usage measurement is on. The terminal header also shows the quality. You can ask Codex to change it, for example, “Set my turn explanation quality to In-depth.”

| Quality | Aliases for `explanation_quality` and the CLI | What the reader gets | Preceding turns available |
| --- | --- | --- | --- |
| Basic | `low`, `기본`, `낮음` | The task, its purpose, and the main outcome | Up to 1 |
| Contextual | `normal`, `맥락`, `보통` | An explanation that connects relevant earlier decisions to the work and its result | Up to 3 |
| In-depth | `high`, `심층`, `높음` | A standalone account of the task, why it took this shape, and what the evidence confirms or leaves open | Up to 6 |

The worker periodically reviews its model choices. Run `agent-journal explanation --refresh` to review the choices immediately. Run `agent-journal explanation-usage --days 30` to see tokens spent generating explanations and reviewing model choices on this machine. Usage measurement controls future recording. The existing counts remain in `explanation-usage.sqlite3` in the local state directory.

### Display time zone

Run `agent-journal timezone` to see the local display time zone. The terminal header shows it, and the TUI turn details and `agent-journal stats` use it for timestamps. The machine default is UTC.

When opened directly, the Web journal uses the browser preference or, by default, the browser time zone. Launching from `agent-journal web` or the TUI applies an explicitly configured machine zone to that browser. With an unset machine zone, the browser keeps its current preference. After `agent-journal timezone default`, the next launch from the client resets the browser to its own time zone as well.

## Browser preferences

| Preference | Default | Change or reset |
| --- | --- | --- |
| Display time zone | Browser time zone | Open the time zone control in the Web header and enter an IANA time zone. The control shows the active zone. Select **Use browser time zone** to reset. Journey and Storage timestamps use this setting. |
| Theme | **System**, which follows the device appearance | Click the monitor, sun, or moon icon in the Web header for **System**, **Light**, or **Dark**. Click the monitor to reset. |

## Server connection

Run `agent-journal connect` to enter a server URL and access token. Use `agent-journal connect --url https://journal.example.com` to supply the URL up front. Once verified, the connection is stored as `url` and `token` in `connection.json` under the local state directory. Re-run the command to change the saved connection. The file contains the access token. Keep it in private storage.

For ordinary client commands, a nonempty `AGENT_JOURNAL_URL` or `AGENT_JOURNAL_TOKEN` takes precedence over the corresponding saved value for that process. Unset the variable to use the saved value again. The client URL falls back to `http://127.0.0.1:8765`. A remote server URL requires HTTPS, and the client requires a token. The explicit URL and token supplied to `connect` take precedence when saving a new connection.

## Installer and terminal options

| Option | Scope and behavior |
| --- | --- |
| `AGENT_JOURNAL_BIN_DIR` | Overrides the standalone installer's default destination, `~/.local/bin`, for that install. |
| `agent-journal tui --no-mouse` | Disables mouse input for that TUI run. Mouse input is enabled by default. |
| `NO_COLOR=1` | Turns off color for that TUI process. Session labels use underlining when color is unavailable. |

## Server settings

The server gets its access token from `AGENT_JOURNAL_TOKEN`. Use [managed installation](OPERATIONS.md#managed-installation) for the service environment file and persistent storage, or [Run a basic server](OPERATIONS.md#run-a-basic-server) for the bind address, port, and database options of a shell-started server. See [Rotate the access token](OPERATIONS.md#rotate-the-access-token) to change a deployed token.
