"""Build a standalone client for the current release platform."""

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    ("darwin", "arm64"): "macos-arm64",
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-arm64",
}


def main():
    target = PLATFORMS.get((sys.platform, platform.machine()))
    if target is None:
        raise SystemExit("Unsupported client release platform")
    name = f"agent-journal-{target}"
    build = ROOT / "build" / name
    dist = ROOT / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--noupx",
            "--noconfirm",
            "--clean",
            "--name",
            name,
            "--workpath",
            str(build),
            "--specpath",
            str(build),
            "--distpath",
            str(dist),
            "--collect-data",
            "agent_journal",
            "--collect-all",
            "tzdata",
            "--copy-metadata",
            "agent-journal",
            str(ROOT / "src" / "agent_journal" / "__main__.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    print(dist / name)


if __name__ == "__main__":
    main()
