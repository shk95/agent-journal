import os
import sys

from agent_journal.cli import main

if __name__ == "__main__":
    if getattr(sys, "frozen", False) and sys.platform == "linux":
        # Keep the bundled libraries out of Git, Codex, and other child processes.
        original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    main()
