# Agent Journal

Agent Journal records Codex sessions from your registered GitHub repositories and syncs them through a central server. Use the terminal journal beside Codex or open the web journal in a browser.

## What you can do

- Follow requests, progress, and results in **Journey**, with a graph that connects concurrent and forked sessions.
- Open a turn to read its explanation above the transcript. Expand either section for a larger view.
- Keep Markdown notes on individual turns or on the project. Ask Codex to read the journal or attach a note to a specific turn.
- Choose **Basic**, **Contextual**, or **In-depth** explanations on each work machine and check the additional tokens they use.

## Get started

1. Use an existing Agent Journal server or [set one up](docs/OPERATIONS.md#managed-installation). On each work machine, [install the standalone client](docs/SETUP.md#standalone-client) and run `agent-journal connect` with the server URL and access token.
2. Sign in to GitHub CLI and [trust the recording hooks in Codex](docs/SETUP.md#set-up-codex). Open a new Codex session in the repository and ask, “Register this repository with Agent Journal.” Register each repository once.
3. Run `agent-journal` from the repository for the terminal client, or `agent-journal web` for the browser. In the terminal, use `Enter` to open a turn, `m` for a turn note, `n` for a project note, and `w` to open the project in the browser.

Run `agent-journal explanation` to see the current quality, `agent-journal explanation In-depth` to change it, or `agent-journal explanation-usage` to see this machine's token usage. The default quality is **Contextual**. [Explanation settings](docs/SETUP.md#explanation-settings)
