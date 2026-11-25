import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.workspace import (
    LOG_TAIL_BYTES,
    REPORT_HEADER,
    RETENTION_SECONDS,
    prune_failure_reports,
    test_workspace as workspace,
)


class WorkspaceRetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.reports = self.root / "reports"

    def test_success_and_cancellation_remove_the_workspace(self):
        with workspace("aj-smoke-", self.reports, self.root) as successful:
            (successful / "fixture").touch()
        self.assertFalse(successful.exists())
        with self.assertRaises(SystemExit):
            with workspace("aj-smoke-", self.reports, self.root) as cancelled:
                raise SystemExit(143)
        self.assertFalse(cancelled.exists())
        self.assertFalse(self.reports.exists())

    def test_failure_keeps_only_private_diagnostics(self):
        with patch("builtins.print"), self.assertRaisesRegex(RuntimeError, "fixture failed"):
            with workspace("aj-smoke-", self.reports, self.root) as run:
                journal = run / "journal"
                journal.mkdir()
                (journal / "observer.log").write_bytes(b"old\n" + b"x" * LOG_TAIL_BYTES)
                (journal / "server.sqlite3").touch()
                (run / "connection.json").touch()
                raise RuntimeError("fixture failed")
        self.assertFalse(run.exists())
        report, = self.reports.iterdir()
        self.assertEqual(report.stat().st_mode & 0o077, 0)
        self.assertEqual({file.name for file in report.iterdir()}, {"failure.txt", "observer.log"})
        self.assertIn("fixture failed", (report / "failure.txt").read_text())
        self.assertEqual((report / "observer.log").read_bytes(), b"x" * LOG_TAIL_BYTES)

    def test_prune_keeps_current_reports_and_other_directories(self):
        expired, fresh, unowned, mixed = [
            self.reports / f"aj-compat-{name}" for name in ("expired", "fresh", "unowned", "mixed")
        ]
        for report in (expired, fresh, unowned, mixed):
            report.mkdir(parents=True)
            (report / "failure.txt").write_text(REPORT_HEADER if report != unowned else "Other notes\n")
        (mixed / "operator-notes.txt").touch()
        old = time.time() - RETENTION_SECONDS - 1
        for report in (expired, unowned, mixed):
            os.utime(report / "failure.txt", (old, old))
        self.assertEqual(prune_failure_reports(self.reports), 1)
        self.assertEqual(set(self.reports.iterdir()), {fresh, unowned, mixed})
