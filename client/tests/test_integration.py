"""Integration tests: Shell + StatusApp + DummyDaemon full stack."""
import json
import os
import tempfile

from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.shell import Shell
from shesha.tui.status import StatusApp, SplashView
from shesha.tui.pytui import Keyboard


def _make_shell():
    """Create a fully wired shell with StatusApp and DummyDaemon."""
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)
    shell = Shell(apps=[StatusApp()], config=config, daemon=daemon)
    return shell, f.name


def test_shell_with_status_app():
    """Shell constructs correctly with StatusApp."""
    shell, path = _make_shell()
    try:
        assert shell._active_app == 'status'
        assert shell.daemon.connected is True
        assert shell.daemon.busy is False
    finally:
        os.unlink(path)


def test_data_flows_to_splash():
    """PSD frames from daemon reach the status splash view."""
    shell, path = _make_shell()
    try:
        # Get the splash view
        app = shell.active_app
        app.switch_view('splash')
        view = app.active_view
        assert isinstance(view, SplashView)

        # Feed frames via the shell method
        frames = shell.daemon.poll()
        assert len(frames) == 1
        shell._feed_frames(frames)

        # Dashboard should have updated its metrics
        assert view._noise_floor != 0.0  # Got real data
        assert view._have_snr
    finally:
        os.unlink(path)


def test_config_shortcut_updates_daemon():
    """Config shortcuts propagate through config -> daemon -> shell."""
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('gain 45')
        # Config has it
        assert shell.config.get('gain') == '45'
        # Daemon state reflects it (dummy applies immediately)
        assert shell.daemon.get_state()['gain'] == '45'
    finally:
        os.unlink(path)


def test_connect_dummy_command():
    """':connect dummy' switches daemon to DummyDaemon."""
    shell, path = _make_shell()
    try:
        # Already dummy, but test the command path works
        shell._exec_command_inner('connect dummy')
        assert isinstance(shell.daemon, DummyDaemon)
        assert shell.daemon.connected is True
    finally:
        os.unlink(path)


def test_freq_tuning_via_keys():
    """Normal mode h/l keys tune frequency."""
    shell, path = _make_shell()
    try:
        shell.config.set('freq', '5180000000')
        shell.daemon.poll()  # confirm any pending

        # Tune up 1 MHz via 'l' key
        shell._handle_key('l')
        state = shell.daemon.get_state()
        freq = int(state['freq'])
        assert freq == 5181000000

        # Tune down 5 MHz: type '5' then 'h'
        shell._handle_key('5')
        shell._handle_key('h')
        state = shell.daemon.get_state()
        freq = int(state['freq'])
        assert freq == 5176000000
    finally:
        os.unlink(path)


def test_command_history_dedup():
    """Consecutive duplicate commands are deduplicated in history."""
    shell, path = _make_shell()
    try:
        shell._push_history('gain 50')
        shell._push_history('gain 50')
        shell._push_history('gain 50')
        assert len(shell._command_history) == 1
        shell._push_history('freq 5200000000')
        assert len(shell._command_history) == 2
    finally:
        os.unlink(path)


def test_tab_completion_global():
    """Tab completion offers global commands."""
    shell, path = _make_shell()
    try:
        candidates = shell._commands.complete('', 0).candidates
        assert 'connect' in candidates
    finally:
        os.unlink(path)


def test_error_display():
    """Shell problems appear and clear on keypress."""
    shell, path = _make_shell()
    try:
        shell._problem('test error')
        assert shell._result_message == 'test error'
        assert shell._result_problem is True
        # In normal mode, next keypress clears it
        shell._handle_key('5')
        assert shell._result_message == ''
    finally:
        os.unlink(path)


def test_animation_full_lifecycle():
    """Full animation lifecycle: send -> pulse -> settle -> done."""
    shell, path = _make_shell()
    try:
        # Apply triggers send pulse + confirmed-change settle
        shell.daemon.apply('freq', '5200000000')
        shell.daemon.drain_notifications()
        assert shell._animator.running('status_freq')

        # Tick past the settle delay (1.0s) + transition (0.3s)
        shell._animator.tick(2.0)
        shell._animator.tick(1.0)
        # Should be done now
        assert not shell._animator.running('status_freq')
    finally:
        os.unlink(path)
