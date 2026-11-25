@_default:
    just --list

# Open an isolated fixture journal in the terminal.
demo-tui:
    #!/bin/sh
    if [ -t 0 ] && [ -t 1 ]; then
        saved_tty=$(stty -g) || exit 1
        restore_tty() {
            status=$?
            stty "$saved_tty"
            if [ "$status" -ne 0 ]; then
                printf '\033[?2004l\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1049l\033[?1l\033>\033[?7h\033[?25h\033[0m'
            fi
        }
        trap restore_tty EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 131' QUIT
        trap 'exit 143' TERM
    fi
    uv run --locked python scripts/dev.py demo tui

# Open an isolated fixture journal in the browser.
demo-web:
    @exec uv run --locked python scripts/dev.py demo web

# Exercise the client and server API against earlier stable tags in this major.
compatibility:
    @exec uv run --locked python -m tests.compatibility

# Remove expired local test failure reports.
clean-test-reports:
    @exec uv run --locked python -m tests.workspace
