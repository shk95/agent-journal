import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.processes import child, group_alive, startup_line, stop_group


class ProcessCleanupTests(unittest.TestCase):
    def test_permission_error_after_process_group_exits(self):
        with (
            patch("tests.processes.os.killpg", side_effect=PermissionError),
            patch("tests.processes.subprocess.check_output", return_value="98765 Z\n"),
        ):
            self.assertFalse(group_alive(98765))

    def test_child_cleanup_stops_group_after_leader_exits(self):
        command = """
import subprocess
import sys

subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print("ready", flush=True)
"""
        with child([sys.executable, "-c", command], stdout=subprocess.PIPE, text=True) as process:
            self.assertEqual(startup_line(process), "ready\n")
            self.assertEqual(process.wait(timeout=2), 0)
            self.assertTrue(group_alive(process.pid))
        self.assertFalse(group_alive(process.pid))

    def test_sigterm_unwinds_nested_process_cleanup(self):
        command = """
import subprocess
import sys
from tests.processes import child, cleanup_on_termination

with cleanup_on_termination(), child([sys.executable, "-c", "import time; time.sleep(60)"]) as process:
    print(process.pid, flush=True)
    process.wait()
"""
        with child([sys.executable, "-c", command], stdout=subprocess.PIPE, text=True) as runner:
            worker = int(startup_line(runner))
            try:
                runner.send_signal(signal.SIGTERM)
                self.assertEqual(runner.wait(timeout=8), 128 + signal.SIGTERM)
                self.assertFalse(group_alive(worker))
            finally:
                stop_group(worker)
