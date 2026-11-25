# Agent Journal first personal release

One person can run Codex as usual on two work machines and read the resulting journey in a terminal pane or a browser. The journal lives on a single cloud server. The release also supports asking Codex to read the journal or add a note. The server, observer, MCP interface, terminal client, and web client share this repository.

## Acceptance criteria

| Workflow | Pass condition |
| --- | --- |
| Installation and registration | Set up one server and connect two work machines. Register a GitHub repository once and record from its clones and worktrees. Automatic collection starts after registration. Disabling collection stops new records and preserves access to existing history. |
| Automatic collection | A regular Codex turn appears once, with its status and summary updating independently. The UI distinguishes completion, interruption, failure, and a lost observer connection. The same row contains the final summary and opens the transcript. |
| Session graph | Follow the continuity and fork point of interleaved sessions in request order. Compaction and resume continue an existing line. A new session or clear starts a separate line. Equal timestamps have a stable ordering. |
| Clients and notes | Select Journey, Turn notes, and Project notes independently in each client. Create and edit both kinds of notes. Incoming turns preserve the currently selected transcript or note target. The web overview shows active sessions and links to the relevant project. |
| Codex access | Codex chooses and reports which axes it read. General progress queries default to Journey. An explicit request to attach a note to the previous result is itself recorded as a turn, and the note lands on the referenced earlier turn. |
| Recovery and operation | After reconnecting, observers deliver queued records once. Server and observer restarts preserve continuity. Verify authenticated HTTPS access, persistent storage, and backup restoration in the deployment environment. |

Use **10 seconds** as the initial target for displaying a request or terminal state while connected. Show observed progress while the model summary is pending. Measure summary latency and model calls in representative sessions before setting a summary target.

## Work before release

| Order | Current state | Definition of done |
| --- | --- | --- |
| 1. Collection and sync | The observer polls every three seconds and queues snapshots in SQLite. It skips interrupted turns with no completion timestamp. Disabling collection can leave earlier turns marked as running. Registration can miss a turn from the same second. | Recheck temporary misreads and make lasting connection loss visible. Recover the actual turn state when the connection returns. Apply consistent registration and disable boundaries at the server and observer. Reproduce disconnects and retries in regression tests. |
| 2. Graph and clients | The web shows session markers and a parent session ID. The API lacks the exact fork turn. The terminal shows no session lines. The web overview polls entire histories, and the clients can create notes. | Store the fork turn and display session continuity and independent starts in both clients. Page long histories and limit the overview to active work and recent results. Support editing both kinds of notes while keeping selection stable during updates. |
| 3. MCP and summaries | Summaries run in a separate process and use some preceding turns. There is no MCP entry point. | Expose scoped reads and explicit note creation through local MCP and report the actual read scope. Match short replies to the preceding request across page boundaries and evaluate whether summaries retain earlier decisions that still apply. |
| 4. Installation and operation | The server, observer, and terminal clients have CLI entry points and README instructions. Startup, observer health, backup restoration, and two-machine cloud use still need verification. | Install from a clean environment using the published instructions. Verify authenticated HTTPS access, restart recovery, two observers sharing one journal, and restoration of turn and note data into a fresh instance. |

### Implementation order

1. Start with turn status and registration in the [observer](src/agent_journal/observer.py) and [server](src/agent_journal/server.py). Separate observer connectivity from turn status. Reject new turns after disabling collection and avoid leaving previously recorded turns marked as running indefinitely. Test delayed and repeated updates.
2. Retain the project, session, and turn IDs as the turn key. Verify the exact fork point from real Codex history using the shared turn IDs. When the fork point is unavailable or predates registration, display the relationship supported by the available records. Preserve existing turns and notes during SQLite migrations.
3. Give MCP reads explicit axes and a result limit. Require a session and turn ID for an attached note. Resolve references in the current session even when another session has a newer turn. Reuse the note ID on retry.
4. Run the acceptance scenarios on two actual work machines against the cloud server. Record supported Codex versions and deployment commands in the operating instructions.

## Acceptance scenarios

- **Registration boundaries** — Create turns immediately before and after registration, then after disabling collection. Use two worktrees and a separate unregistered repository. Verify which turns appear.
- **Interleaved work** — Start two turns on different machines and finish the later request first. Fork one session. Compact, resume, and clear the other. Check the execution states and session lines in both clients and record display latency.
- **Transcripts and notes** — Create a turn note in the terminal and edit it in the browser. Create a project note in the browser and read it in the terminal. Ask Codex to read Journey, then attach a note to an earlier result. Verify the reported scope, recorded instruction, and note target.
- **Outage and restoration** — Make a request while the server is unreachable and restart the observer. Restore connectivity and verify each turn and transcript arrives once. Distinguish observer disconnection from turn interruption, then restore turns and notes from a backup.

## Follow-up work

Selective import of older Codex sessions, collaboration, repository renames, project-specific settings, a dedicated view for open agent questions, and advanced browser interactions follow the first personal release.

## Development baseline

The baseline was verified on Codex CLI 0.172.0 with seven automated tests, a JavaScript syntax check, and successful source and wheel builds. The state and cutoff defects were reproduced with temporary SQLite files and synthetic input. Run the cloud acceptance scenarios with two work machines before calling the release complete.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
node --check src/agent_journal/static/app.js
uv build
```
