"""Check the public file inventory and Git metadata without displaying file contents."""

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import PurePosixPath

ROOT_FILES = {
    ".gitignore",
    ".env.example",
    "AGENTS.md",
    "README.md",
    "RELEASE_PLAN.md",
    "pyproject.toml",
    "uv.lock",
}
SOURCE_FILES = (
    "src/agent_journal/*.py",
    "src/agent_journal/static/*.html",
    "src/agent_journal/static/*.js",
    "src/agent_journal/static/*.css",
    "tests/*.py",
    "scripts/*.py",
    "docs/*.md",
    ".github/workflows/*.yml",
)
LOCAL_VALUE = re.compile(
    rb"(?:/(?:Users|home|private/tmp)/[A-Za-z0-9_.-]+"
    rb"|[A-Z]:[\\\\]Users[\\\\][A-Za-z0-9_.-]+"
    rb"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
    rb"|https?://[^/\s:@]+:[^/\s@]+@)"
)
NOREPLY = re.compile(rb"[A-Za-z0-9._+-]+@users\.noreply\.github\.com")


def git(*args):
    return subprocess.check_output(["git", *args])


def entries(revision):
    if revision:
        raw = git("ls-tree", "-r", "-z", revision)
        for entry in raw.split(b"\0"):
            if entry:
                meta, path = entry.split(b"\t", 1)
                mode, _, oid = meta.split()
                yield mode, oid, path
    else:
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                meta, path = entry.split(b"\t", 1)
                mode, oid, _ = meta.split()
                yield mode, oid, path


def check(revisions):
    failures, checked = set(), set()
    for revision in revisions:
        if revision:
            author, committer, author_date, commit_date, message = git(
                "show", "-s", "--format=%ae%x00%ce%x00%aI%x00%cI%x00%B", revision
            ).split(b"\0", 4)
            if not all(NOREPLY.fullmatch(value) for value in (author, committer)):
                failures.add(
                    f"{revision[:12]}: use noreply author and committer emails"
                )
            if not all(
                value.endswith((b"Z", b"+00:00"))
                for value in (author_date, commit_date)
            ):
                failures.add(f"{revision[:12]}: use UTC commit dates")
            if LOCAL_VALUE.search(message):
                failures.add(
                    f"{revision[:12]}: review a local value in the commit message"
                )
        for mode, oid, raw_name in entries(revision):
            name = raw_name.decode(errors="replace")
            path = PurePosixPath(name)
            if (
                mode not in (b"100644", b"100755")
                or not (
                    name in ROOT_FILES
                    or any(
                        name.count("/") == item.count("/")
                        and fnmatch.fnmatchcase(name, item)
                        for item in SOURCE_FILES
                    )
                )
                or any(part.startswith(".") for part in path.parts[1:])
            ):
                failures.add(f"{name!r}: outside the public file inventory")
                continue
            if oid in checked:
                continue
            checked.add(oid)
            data = git("cat-file", "blob", oid.decode())
            if len(data) > 512 * 1024 or b"\0" in data:
                failures.add(f"{name!r}: expected a small text file")
            elif LOCAL_VALUE.search(data):
                failures.add(f"{name!r}: review a local path or credential pattern")
            else:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    failures.add(f"{name!r}: expected UTF-8 text")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    revisions = (
        git("rev-list", "refs/heads/master").decode().split()
        if args.history
        else [None]
    )
    failures = check(revisions)
    if failures:
        print("\n".join(sorted(failures)), file=sys.stderr)
        return 1
    print("Public file inventory and local-value checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
