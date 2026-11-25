# Open an isolated fixture journal in the terminal or browser.
demo client="tui":
    @exec uv run --locked python scripts/dev.py demo {{quote(client)}}
