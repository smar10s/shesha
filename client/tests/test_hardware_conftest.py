"""Unit tests for the hardware daemon gate.

These run without a Pluto.  The gate must compare the daemon's build id with
the worktree HEAD so a suite cannot pass against a stale binary.
"""
import subprocess

import pytest

import tests.hardware.conftest as hw


class Reporter:
    def __init__(self):
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)


def _head_build_id() -> int:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=hw.REPO_ROOT, text=True).strip()
    return int(head[:8], 16)


def test_expected_fw_build_id_uses_head():
    assert hw._expected_fw_build_id() == _head_build_id()


def test_ensure_daemon_queries_build_when_port_is_open(monkeypatch):
    expected = _head_build_id()
    queried = []

    monkeypatch.setattr(hw, "_daemon_port_open", lambda *a, **k: True)
    monkeypatch.setattr(hw, "_expected_fw_build_id", lambda: expected)

    def fake_daemon_build_id():
        queried.append(True)
        return expected

    monkeypatch.setattr(hw, "_daemon_fw_build_id", fake_daemon_build_id)

    calls = []
    monkeypatch.setattr(hw, "_make", lambda target, reporter: calls.append(target))

    hw._ensure_daemon(Reporter())

    assert queried == [True]
    assert calls == []


def test_ensure_daemon_deploys_stale_daemon(monkeypatch):
    expected = _head_build_id()
    board_ids = iter([0x08BEB395, expected])

    monkeypatch.setattr(hw, "_daemon_port_open", lambda *a, **k: True)
    monkeypatch.setattr(hw, "_expected_fw_build_id", lambda: expected)
    monkeypatch.setattr(hw, "_daemon_fw_build_id", lambda: next(board_ids))

    calls = []
    monkeypatch.setattr(hw, "_make", lambda target, reporter: calls.append(target))
    reporter = Reporter()

    hw._ensure_daemon(reporter)

    assert calls == ["deploy", "run"]
    assert any("stale daemon" in line for line in reporter.lines)


def test_ensure_daemon_deploys_when_caps_query_fails(monkeypatch):
    expected = _head_build_id()
    board_ids = iter([None, expected])

    monkeypatch.setattr(hw, "_daemon_port_open", lambda *a, **k: True)
    monkeypatch.setattr(hw, "_expected_fw_build_id", lambda: expected)
    monkeypatch.setattr(hw, "_daemon_fw_build_id", lambda: next(board_ids))

    calls = []
    monkeypatch.setattr(hw, "_make", lambda target, reporter: calls.append(target))

    hw._ensure_daemon(Reporter())

    assert calls == ["deploy", "run"]


def test_ensure_daemon_exits_when_still_stale_after_deploy(monkeypatch):
    expected = _head_build_id()

    monkeypatch.setattr(hw, "_daemon_port_open", lambda *a, **k: True)
    monkeypatch.setattr(hw, "_expected_fw_build_id", lambda: expected)
    monkeypatch.setattr(hw, "_daemon_fw_build_id", lambda: 0x08BEB395)

    calls = []
    monkeypatch.setattr(hw, "_make", lambda target, reporter: calls.append(target))

    with pytest.raises(pytest.exit.Exception) as exc:
        hw._ensure_daemon(Reporter())

    assert calls == ["deploy", "run"]
    assert "stale daemon" in str(exc.value)


def test_ensure_daemon_cold_boot_still_deploys_and_verifies(monkeypatch):
    expected = _head_build_id()
    port_results = iter([False, False, True])

    monkeypatch.setattr(hw, "_daemon_port_open", lambda *a, **k: next(port_results))
    monkeypatch.setattr(hw, "_board_reachable", lambda: True)
    monkeypatch.setattr(hw, "_daemon_running", lambda: False)
    monkeypatch.setattr(hw, "_daemon_installed", lambda: False)
    monkeypatch.setattr(hw, "_expected_fw_build_id", lambda: expected)

    queried = []

    def fake_daemon_build_id():
        queried.append(True)
        return expected

    monkeypatch.setattr(hw, "_daemon_fw_build_id", fake_daemon_build_id)

    calls = []
    monkeypatch.setattr(hw, "_make", lambda target, reporter: calls.append(target))

    hw._ensure_daemon(Reporter())

    assert calls == ["deploy", "run"]
    assert queried == [True]
