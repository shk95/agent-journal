"""Keep limited, private diagnostics for failed local integration runs."""

import shutil
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path


REPORTS = Path(__file__).resolve().parents[1] / ".local" / "test-failures"
RUN_PREFIXES = ("aj-smoke-", "aj-performance-", "aj-compat-")
RETENTION_SECONDS = 24 * 60 * 60
REPORT_HEADER = "Agent Journal local test failure\n"
REPORT_FILES = {"failure.txt", "observer.log"}
LOG_TAIL_BYTES = 64 * 1024


def prune_failure_reports(directory=REPORTS):
    if not directory.is_dir():
        return 0
    cutoff = time.time() - RETENTION_SECONDS
    removed = 0
    for path in directory.iterdir():
        try:
            if path.is_symlink() or not path.is_dir() or not path.name.startswith(RUN_PREFIXES):
                continue
            marker = path / "failure.txt"
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_mtime > cutoff:
                continue
            with marker.open(encoding="utf-8", errors="replace") as stream:
                if stream.readline(len(REPORT_HEADER) + 1) != REPORT_HEADER:
                    continue
            if set(entry.name for entry in path.iterdir()) <= REPORT_FILES:
                shutil.rmtree(path)
                removed += 1
        except FileNotFoundError:
            # Another integration runner can prune the same report.
            continue
    return removed


def store_failure_report(directory, run, error):
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    report = Path(tempfile.mkdtemp(prefix=run.name + "-", dir=directory))
    (report / "failure.txt").write_text(
        REPORT_HEADER + "".join(traceback.format_exception(error)), encoding="utf-8"
    )
    journal = run / "journal"
    observer_log = journal / "observer.log"
    if not journal.is_symlink() and observer_log.is_file() and not observer_log.is_symlink():
        with observer_log.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - LOG_TAIL_BYTES))
            (report / "observer.log").write_bytes(stream.read(LOG_TAIL_BYTES))
    return report


@contextmanager
def test_workspace(prefix, reports=REPORTS, temp_parent="/tmp"):
    try:
        prune_failure_reports(reports)
    except OSError as error:
        print(f"Could not prune local test reports: {error}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=temp_parent) as folder:
        run = Path(folder)
        try:
            yield run
        except Exception as error:
            try:
                report = store_failure_report(reports, run, error)
            except OSError as report_error:
                error.add_note(f"Could not save local test diagnostics: {report_error}")
            else:
                print(f"Local test diagnostics saved in {report}", file=sys.stderr)
            raise


if __name__ == "__main__":
    print(f"Expired test reports removed: {prune_failure_reports()}")
