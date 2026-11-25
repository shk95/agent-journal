# Open an isolated fixture journal in the terminal or browser.
demo client="tui":
    @exec uv run --locked python scripts/dev.py demo {{quote(client)}}

# Exercise the client and server API against earlier stable tags in this major.
compatibility:
    @exec uv run --locked python -m tests.compatibility
