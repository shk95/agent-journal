# Agent Journal first personal release

One person can run Codex as usual on two work machines and read the resulting journey in a terminal pane or a browser. The journal lives on a single cloud server. The release also supports asking Codex to read the journal or add a note. The server, observer, MCP interface, terminal client, and web client share this repository.

## Acceptance criteria

| Workflow | Pass condition |
| --- | --- |
| Installation and registration | Set up one server and connect two work machines. Register a GitHub repository once and record from its clones and worktrees. Automatic collection starts after registration. Disabling collection stops new records and preserves access to existing history. |
| Automatic collection | A regular Codex turn appears once, with its status and explanation updating independently. The UI distinguishes completion, interruption, failure, and a lost observer connection. The Journey row shows a brief line and opens the explanation and transcript. |
| Session graph | Follow the continuity and fork point of interleaved sessions in request order. Compaction and resume continue an existing line. A new session or clear starts a separate line. Equal timestamps have a stable ordering. |
| Clients and notes | Select Journey, Turn notes, and Project notes independently in each client. Create and edit both kinds of notes. Incoming turns preserve the currently selected transcript or note target. The web overview shows active sessions and links to the relevant project. |
| Codex access | Codex chooses and reports which axes it read. General progress queries default to Journey. An explicit request to attach a note to the previous result is itself recorded as a turn, and the note lands on the referenced earlier turn. |
| Recovery and operation | After reconnecting, observers deliver queued records once. Server and observer restarts preserve continuity. Verify authenticated HTTPS access, persistent storage, and backup restoration in the deployment environment. |

Use **10 seconds** as the initial target for displaying a request or terminal state while connected. Show observed progress while the explanation is pending. Use local token counts and latency measurements from representative sessions before setting an explanation target.

## Implementation status

| Area | Implemented | Acceptance evidence |
| --- | --- | --- |
| Collection and sync | The observer queues snapshots in SQLite, sends heartbeats for long turns, and distinguishes observation loss from interruption. Both endpoints use the same registration cutoff. | Integration tests exercise same-second registration, transient interruption, disabling collection, two independent observers, reconnection, and replay. |
| Graph and clients | Both clients render the same graph with fork anchors, use bounded journey pages, and support versioned edits for both kinds of notes. The overview fetches current and recent work. | Tests cover shared fork turns, pagination, editing, and conflict detection. |
| MCP and explanations | Local MCP reads selected axes, writes explicit notes using target IDs, and can change the local explanation quality. The worker uses preceding turns across page boundaries and records local token usage. | Tests cover MCP over stdio, scoped reads, note retries, previous-turn targeting, and explanation quality and token counts. |
| Installation and operation | Standalone clients are published with checksums. The cloud guide covers TLS, service startup, updates, and backup restoration. | Local integration tests restore a backup and exercise startup, updates, rollback, and boot recovery. |

## Acceptance scenarios

- **Registration boundaries** — Create turns immediately before and after registration, then after disabling collection. Use two worktrees and a separate unregistered repository. Verify which turns appear.
- **Interleaved work** — Start two turns on different machines and finish the later request first. Fork one session. Compact, resume, and clear the other. Check the execution states and session lines in both clients and record display latency.
- **Transcripts and notes** — Create a turn note in the terminal and edit it in the browser. Create a project note in the browser and read it in the terminal. Ask Codex to read Journey, then attach a note to an earlier result. Verify the reported scope, recorded instruction, and note target.
- **Outage and restoration** — Make a request while the server is unreachable and restart the observer. Restore connectivity and verify each turn and transcript arrives once. Distinguish observer disconnection from turn interruption, then restore turns and notes from a backup.

## Follow-up work

Selective import of older Codex sessions, collaboration, repository renames, project-specific settings, a dedicated view for open agent questions, and advanced browser interactions follow the first personal release.

## Development baseline

The local App Server adapter was exercised with Codex CLI 0.172.0. The automated checks run against temporary databases and synthetic fixtures. Use the [setup guide](docs/SETUP.md) to start the local stack and the [cloud guide](docs/OPERATIONS.md) for deployment.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
uv build
```
