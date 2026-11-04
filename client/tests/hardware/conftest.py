"""Hardware test fixtures and daemon lifecycle.

The Pluto is required.  A cold boot leaves the daemon absent from tmpfs, and
re-plugging USB can take the network down for a moment.  Neither is a crash.
The collection hook lazily deploys and starts the daemon, then fails hard if
it still cannot be reached.
"""
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

PLUTO_HOST = os.environ.get("PLUTO_IP", "192.168.2.1")
PLUTO_PORT = int(os.environ.get("DAEMON_PORT", "5555"))
PLUTO_USER = os.environ.get("PLUTO_USER", "root")
PLUTO_PASS = os.environ.get("PLUTO_PASS", "analog")
DAEMON_PATH = "/usr/bin/shesha-daemon"

CONNECT_TIMEOUT = 2.0
BOOT_TIMEOUT = 30.0
START_TIMEOUT = 5.0
REPO_ROOT = Path(__file__).resolve().parents[3]


def _daemon_port_open(timeout: float = CONNECT_TIMEOUT) -> bool:
    try:
        s = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=timeout)
        s.close()
        return True
    except (OSError, TimeoutError):
        return False


def _ssh(command: str, timeout: float = 20.0):
    argv = [
        "sshpass", "-p", PLUTO_PASS,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=3",
        f"{PLUTO_USER}@{PLUTO_HOST}", command,
    ]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _board_reachable() -> bool:
    result = _ssh("true", timeout=5.0)
    return result is not None and result.returncode == 0


def _daemon_installed() -> bool:
    result = _ssh(f"test -x {DAEMON_PATH}")
    return result is not None and result.returncode == 0


def _daemon_running() -> bool:
    result = _ssh("pidof shesha-daemon >/dev/null")
    return result is not None and result.returncode == 0


def _make(target: str, reporter) -> None:
    result = subprocess.run(
        ["make", target], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode == 0:
        return
    if reporter:
        reporter.write_line(result.stdout)
        reporter.write_line(result.stderr)
    pytest.exit(
        f"make {target} failed. Fix the build/deploy, then rerun.",
        returncode=1)


def _expected_fw_build_id() -> int:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    return int(head[:8], 16)


def _daemon_fw_build_id() -> int | None:
    from shesha.client import DaemonClient

    client = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    try:
        client.connect()
        caps = client.caps()
    except Exception:
        return None
    finally:
        client.disconnect()
    return caps.fw_build_id if caps is not None else None


def _format_build_id(value: int | None) -> str:
    return "unknown" if value is None else f"0x{value:08x}"


def _wait_for_port(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _daemon_port_open():
            return True
        time.sleep(0.25)
    return False


def _cold_start_daemon(reporter) -> None:
    """Wait for the board, then deploy and start a missing daemon.

    A missing daemon is expected after a cold boot (tmpfs) or a USB re-plug.
    Do not assume it crashed; retry the board before concluding anything.
    """
    if reporter:
        reporter.write_line(
            f"daemon not listening on {PLUTO_HOST}:{PLUTO_PORT}; "
            f"checking the board (cold boot or USB re-plug)")

    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if _daemon_port_open():
            return
        if not _board_reachable():
            time.sleep(1.0)
            continue

        if _daemon_running():
            time.sleep(1.0)
            continue

        if not _daemon_installed():
            if reporter:
                reporter.write_line(
                    "shesha-daemon is not installed (tmpfs cold boot); "
                    "deploying with make deploy")
            _make("deploy", reporter)

        if reporter:
            reporter.write_line("starting shesha-daemon with make run")
        _make("run", reporter)

        if _wait_for_port(START_TIMEOUT):
            return

    pytest.exit(
        f"Pluto daemon unavailable at {PLUTO_HOST}:{PLUTO_PORT} after "
        f"{BOOT_TIMEOUT:.0f}s. The board may be booting, or its USB/network "
        f"connection may be unplugged. This is not assumed to be a crash. "
        f"Check power and cabling, then rerun.",
        returncode=1)


def _ensure_daemon(reporter) -> None:
    """Make sure a live daemon matches the current worktree's HEAD.

    A reachable daemon is not enough: it may be a binary left over from an
    earlier commit.  make deploy builds the current worktree, including
    uncommitted changes, so the build id is the only way to tell a fresh
    binary from a stale one.
    """
    if not _daemon_port_open():
        _cold_start_daemon(reporter)

    expected = _expected_fw_build_id()
    actual = _daemon_fw_build_id()
    if actual == expected:
        return

    if reporter:
        reporter.write_line(
            f"stale daemon: board {_format_build_id(actual)}, "
            f"expected {_format_build_id(expected)}; "
            f"deploying the current worktree")

    _make("deploy", reporter)
    _make("run", reporter)
    if not _wait_for_port(START_TIMEOUT):
        pytest.exit(
            f"daemon did not start on {PLUTO_HOST}:{PLUTO_PORT} after deploy",
            returncode=1)

    actual = _daemon_fw_build_id()
    if actual != expected:
        pytest.exit(
            f"stale daemon after deploy: board {_format_build_id(actual)}, "
            f"expected {_format_build_id(expected)}. make deploy builds the "
            f"current worktree, so check that build/shesha-daemon was copied "
            f"and the daemon restarted.",
            returncode=1)


def pytest_collection_modifyitems(config, items):
    """Prepare the board before running hardware tests."""
    hardware = [item for item in items if "hardware" in str(item.fspath)]
    if not hardware:
        return
    reporter = config.pluginmanager.getplugin("terminalreporter")
    _ensure_daemon(reporter)


@pytest.fixture
def daemon():
    """DaemonClient connection scoped to one test.

    Function scope matters: ``stream()`` subscribes to every data type,
    including IQ.  A subscription left open counts against the daemon's
    IQ duplex budget and makes a later TX test fail for the wrong reason.
    """
    from shesha.client import DaemonClient
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    c.disconnect()
