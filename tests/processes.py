"""Own and reap the processes used by the local integration tests."""

import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def cleanup_on_termination():
    """Let the runner unwind its cleanup scopes on the first termination signal."""
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise SystemExit(128 + signum)

    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def group_alive(group, process=None):
    if process is not None:
        process.poll()
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can report EPERM when the last process exits during this check.
        pass
    rows = subprocess.check_output(["ps", "-Ao", "pgid=,stat="], text=True, timeout=5)
    # An adopted zombie can remain until the system reaps it.
    return any(
        int(pgid) == group and not status.startswith("Z")
        for pgid, status in (row.split() for row in rows.splitlines() if row.strip())
    )


def stop_group(group, process=None):
    if group <= 1 or group == os.getpgrp():
        raise ValueError("Expected an isolated test process group")
    for sig, grace in ((signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        if not group_alive(group, process):
            break
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            if group_alive(group, process):
                raise
            break
        deadline = time.monotonic() + grace
        while group_alive(group, process) and time.monotonic() < deadline:
            time.sleep(0.05)
    if process is not None:
        process.wait(timeout=2)
    if group_alive(group):
        raise RuntimeError(f"Test process group {group} did not stop")


@contextmanager
def child(command, **options):
    process = subprocess.Popen(command, start_new_session=True, **options)
    try:
        yield process
    finally:
        try:
            stop_group(process.pid, process)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def startup_line(process, timeout=15):
    stream = process.stdout
    fd = stream.fileno()
    data = bytearray()
    deadline = time.monotonic() + timeout
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise RuntimeError("Timed out waiting for the test server to start")
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 16_384:
            raise RuntimeError("The test server produced an invalid startup line")
    line = data.splitlines(keepends=True)
    return line[0].decode(stream.encoding) if line else ""


def isolated_groups(pids):
    groups = set()
    for pid in pids:
        try:
            group = os.getpgid(pid)
            if group == os.getsid(pid) and group > 1 and group != os.getpgrp():
                groups.add(group)
        except ProcessLookupError:
            pass
    return groups


def observer_groups(directory):
    """Find detached observers by their working directory in a private test run."""
    directory = directory.resolve()
    if not directory.is_dir():
        return set()
    if sys.platform == "linux":
        pids = []
        for process in Path("/proc").glob("[0-9]*"):
            try:
                if (process / "cwd").readlink() == directory:
                    pids.append(int(process.name))
            except OSError:
                continue
    else:
        result = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-t", str(directory)],
            check=False, capture_output=True, text=True, timeout=5,
        )
        if result.returncode not in (0, 1) or result.stderr:
            raise RuntimeError("Could not inspect the isolated observer directory")
        pids = [int(pid) for pid in result.stdout.split()]
    return isolated_groups(pids)


def stop_tmux(tmux, session):
    panes = subprocess.run(
        tmux + ["list-panes", "-a", "-F", "#{pid} #{pane_pid}"],
        check=False, capture_output=True, text=True, timeout=5,
    )
    if panes.returncode and "no server running" not in panes.stderr and "error connecting" not in panes.stderr:
        raise RuntimeError(f"Could not inspect the test tmux server\n{panes.stderr.strip()}")
    groups = isolated_groups(map(int, panes.stdout.split()))
    if not panes.returncode:
        try:
            subprocess.run(tmux + ["send-keys", "-t", session, "Escape", "Escape", "q"],
                           check=False, capture_output=True, timeout=2)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and any(group_alive(group) for group in groups):
                time.sleep(0.05)
            subprocess.run(tmux + ["kill-server"], check=False, capture_output=True, timeout=2)
        finally:
            for group in groups:
                stop_group(group)
