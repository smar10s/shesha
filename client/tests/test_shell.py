"""Shell tests — headless, no actual terminal."""
import json
import os
import tempfile
import threading
import pytest
from shesha.tui.config import Config
from shesha.tui.daemon import SheshaDaemon
from shesha.tui.dummy import DummyDaemon
from shesha.tui.registry import APP_CLASSES
from shesha.tui.shell import Shell
from shesha.tui.app import App, View
from shesha.tui.pytui import Keyboard
from shesha.tui.theme import STYLES


class DummyView(View):
    name = 'test'
    def __init__(self):
        super().__init__()
        self.commands_received = []

    def command(self, args):
        self.commands_received.append(args)
        return (None, '')


class DummyApp(App):
    name = 'test'
    default_view = 'test'
    def __init__(self):
        super().__init__()
        self.views = {'test': DummyView()}


def _make_shell():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)
    app = DummyApp()
    shell = Shell(apps=[app], config=config, daemon=daemon)
    return shell, f.name


def test_construction():
    shell, path = _make_shell()
    try:
        assert shell._active_app == 'test'
        assert shell.daemon.connected is True
        assert shell._animator.running('daemon')
    finally:
        os.unlink(path)


def test_app_validate_rejects_no_views():
    class EmptyApp(App):
        name = 'empty'
        default_view = 'main'

    with pytest.raises(ValueError, match='no views'):
        EmptyApp().validate()


def test_app_validate_rejects_unknown_default_view():
    class WrongApp(App):
        name = 'wrong'
        default_view = 'nope'

        def __init__(self):
            super().__init__()
            self.views = {'main': DummyView()}

    with pytest.raises(ValueError, match='default_view'):
        WrongApp().validate()


def test_shell_rejects_app_with_bad_default_view():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)

    class WrongApp(App):
        name = 'wrong'
        default_view = 'nope'

        def __init__(self):
            super().__init__()
            self.views = {'main': DummyView()}

    try:
        with pytest.raises(ValueError, match='default_view'):
            Shell(apps=[WrongApp()], config=config, daemon=daemon)
    finally:
        os.unlink(f.name)


def test_config_shortcut():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('gain 50')
        assert shell.config.get('gain') == '50'
    finally:
        os.unlink(path)


def test_set_command():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('set freq 5200000000')
        assert shell.config.get('freq') == '5200000000'
    finally:
        os.unlink(path)


def test_active_app_child_resolves_without_app_prefix():
    from shesha.tui.shell import Shell
    from shesha.tui.wifi import WifiApp

    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        shell = Shell(apps=[WifiApp()], config=config,
                      daemon=DummyDaemon(config))
        shell._exec_command_inner('scan')
        assert shell.active_app.active_view.name == 'scan'
    finally:
        os.unlink(f.name)


def test_global_stop_tx_resolves_anywhere():
    shell, path = _make_shell()
    called = []
    try:
        shell.daemon.tx_stop = lambda: called.append(True)
        shell._exec_command_inner('stop_tx')
        assert called == [True]
    finally:
        os.unlink(path)


def test_set_and_get_still_work():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('set freq 5200000000')
        assert shell.config.get('freq') == '5200000000'
        shell._exec_command_inner('get freq')
        assert '5200000000' in shell._result_message
    finally:
        os.unlink(path)


def test_rate_allowed_inside_daemon_policy():
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(2)          # IQ: independent rate
        shell._apply_config('sample_rate', '3000000')
        assert shell.config.get('sample_rate') == '3000000'
        assert shell.daemon.get_state()['sample_rate'] == '3000000'
    finally:
        os.unlink(path)


def test_rate_refused_outside_daemon_policy():
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(2)          # IQ: 2.1-4.0 MSPS
        before = shell.daemon.get_state()['sample_rate']
        shell._apply_config('sample_rate', '5000000')
        assert shell.daemon.get_state()['sample_rate'] == before
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_derived_rate_refused_in_waterfall():
    """Waterfall derives sr from bw; an explicit rate is not an input."""
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(0)
        shell._apply_config('sample_rate', '2500000')
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_fixed_bw_refused_in_fm():
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(3)          # FM: bw fixed at 200 kHz
        shell._apply_config('bw', '2000000')
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_gain_refused_when_fpga_pins_agc():
    """wifi_fpga owns the AGC; a gain change must be refused up front."""
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(1)          # wifi
        shell.daemon.set_fpga(True)       # fpga: AGC pinned
        shell._apply_config('gain', '30')
        assert shell._result_problem is True
        assert shell.daemon.get_state().get('gain') != '30'
    finally:
        os.unlink(path)


def test_gain_allowed_in_software_wifi():
    """Only the FPGA producer pins AGC; software wifi stays configurable."""
    shell, path = _make_shell()
    try:
        shell.daemon.set_mode(1)          # wifi, fpga off
        shell._apply_config('gain', '30')
        assert shell._result_problem is False
        assert shell.daemon.get_state()['gain'] == '30'
    finally:
        os.unlink(path)


def test_set_symbolic_value_reaches_daemon_normalized():
    """`:set viz.waterfall.window hann` must send the wire number, not 'hann'.

    The daemon builds CMD_SET_WINDOW with int(value); the symbolic name raised
    there.  Config normalizes on set, but the shell forwarded the raw text.
    """
    from shesha.tui.viz import VizApp
    added = VizApp.config_keys()
    Config.register_keys(added)
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('set viz.waterfall.window hann')
        assert shell.config.get('viz.waterfall.window') == '1'
        assert shell.daemon.get_state()['window'] == '1'
    finally:
        for key in added:
            Config._schema.pop(key, None)
        os.unlink(path)


def test_quit():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('quit')
        assert shell._running is False
    finally:
        os.unlink(path)


def test_run_shuts_down_when_the_loop_raises(monkeypatch):
    """An unhandled loop exception must still stop TX and restore state."""
    shell, path = _make_shell()
    calls = []
    try:
        monkeypatch.setattr(shell.screen.keyboard, 'listen',
                            lambda *a, **k: None)
        monkeypatch.setattr(shell.screen, 'start',
                            lambda: calls.append('start'))
        monkeypatch.setattr(shell.screen, 'stop',
                            lambda: calls.append('stop'))
        monkeypatch.setattr(shell.screen, 'layout', lambda fn: None)
        monkeypatch.setattr(shell.screen, 'draw', lambda: None)
        monkeypatch.setattr(shell, 'shutdown',
                            lambda: calls.append('shutdown'))

        class _BoomLoop:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, callbacks):
                raise RuntimeError('boom')

        monkeypatch.setattr('shesha.tui.shell.EventLoop', _BoomLoop)
        with pytest.raises(RuntimeError, match='boom'):
            shell.run()
        assert calls == ['start', 'shutdown', 'stop']
    finally:
        os.unlink(path)


def test_shutdown_closes_recording_and_runs_once(tmp_path):
    shell, path = _make_shell()
    exits = []
    try:
        shell.active_app.switch_view('test')
        shell.active_app.active_view.on_exit = lambda: exits.append(True)
        rec = tmp_path / 'capture.pcap'
        handle = rec.open('wb')
        shell._rec_file = handle
        shell._recording = True

        shell.shutdown()
        assert shell._rec_file is None
        assert not shell._recording
        assert handle.closed

        shell.shutdown()
        assert exits == [True]
    finally:
        os.unlink(path)


def test_shutdown_calls_view_on_exit():
    """Quit teardown must run the active view's on_exit, so a view-owned
    TX stream (e.g. FM) is stopped rather than orphaned."""
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)

    class ExitingView(View):
        name = 'test'

        def __init__(self):
            super().__init__()
            self.exited = False

        def on_exit(self):
            self.exited = True

    class ExitingApp(App):
        name = 'test'
        default_view = 'test'

        def __init__(self):
            super().__init__()
            self.views = {'test': ExitingView()}

    try:
        app = ExitingApp()
        shell = Shell(apps=[app], config=config, daemon=daemon)
        view = app.views['test']
        shell._sync_view(view)
        app.switch_view('test')
        shell.shutdown()
        assert view.exited
    finally:
        os.unlink(f.name)


def test_history():
    shell, path = _make_shell()
    try:
        shell._push_history('gain 50')
        shell._push_history('freq 5200000000')
        assert len(shell._command_history) == 2
        shell._input.command_buf = ''
        shell._history_back()
        assert shell._input.command_buf == 'freq 5200000000'
        shell._history_back()
        assert shell._input.command_buf == 'gain 50'
        shell._history_forward()
        assert shell._input.command_buf == 'freq 5200000000'
    finally:
        os.unlink(path)


def test_sent_starts_pulse():
    shell, path = _make_shell()
    try:
        shell.daemon.apply('gain', '50')
        shell.daemon.drain_notifications()
        assert shell._animator.running('status_gain')
    finally:
        os.unlink(path)


def test_change_settles_pulse():
    shell, path = _make_shell()
    try:
        shell.daemon.apply('gain', '50')
        shell.daemon.drain_notifications()
        assert shell._animator.running('status_gain')
        # First tick: advances pulse past settle_at, replaces with to() anim
        shell._animator.tick(2.0)
        # Second tick: advances to() past its 0.3s duration, completing it
        shell._animator.tick(1.0)
        assert not shell._animator.running('status_gain')
    finally:
        os.unlink(path)


def test_ack_settles_gain_pulse_without_gain_change():
    """`:set gain slow` changes only agc_mode — the pulse must settle on
    the command ack, not on a 'gain' state change (which never fires)."""
    shell, path = _make_shell()
    try:
        shell._animator.pulse('status_gain', frequency=1.0, min_val=0.4,
                              max_val=1.0, phase_offset=0.25)
        assert shell._animator.running('status_gain')
        # Daemon acks the SET_AGC (gain_db unchanged -> no 'gain' notify)
        shell.daemon._last_acked = 'gain'
        shell.daemon._notify('_acked')
        shell.daemon.drain_notifications()
        shell._animator.tick(2.0)
        shell._animator.tick(1.0)
        assert not shell._animator.running('status_gain')
    finally:
        os.unlink(path)


def test_connection_state_animations():
    shell, path = _make_shell()
    try:
        # Busy: fast pulse, regardless of link state.
        with shell.daemon._doing('connect'):
            shell.daemon.drain_notifications()
            assert shell._animator.running('daemon')
        # Idle and connected: slow throb.
        shell.daemon.drain_notifications()
        assert shell.daemon.connected is True
        assert shell._animator.running('daemon')
        # Idle and disconnected: no animation, steady warn.
        shell.daemon._set_connected(False)
        shell.daemon.drain_notifications()
        assert not shell._animator.running('daemon')
    finally:
        os.unlink(path)


def test_command_mode_toggle():
    shell, path = _make_shell()
    try:
        shell._handle_key(':')
        assert shell._input.command_mode is True
        shell._handle_key(Keyboard.ESCAPE)
        assert shell._input.command_mode is False
    finally:
        os.unlink(path)


def test_normal_mode_digits():
    shell, path = _make_shell()
    try:
        shell._handle_key('5')
        shell._handle_key('0')
        assert shell._input.count_buf == '50'
        shell._handle_key(Keyboard.ESCAPE)
        assert shell._input.count_buf == ''
    finally:
        os.unlink(path)


def test_space_reaches_view_keybinds():
    """A view binding for Space must fire: Space is not a global key."""
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)

    class SpaceView(View):
        name = 'test'

        def __init__(self):
            super().__init__()
            self.presses = 0

        def _key_defs(self):
            return [(' ', 'Act', lambda c: setattr(
                self, 'presses', self.presses + 1))]

    class SpaceApp(App):
        name = 'test'
        default_view = 'test'

        def __init__(self):
            super().__init__()
            self.views = {'test': SpaceView()}

    try:
        app = SpaceApp()
        shell = Shell(apps=[app], config=config, daemon=daemon)
        shell._sync_view(app.views['test'])
        app.switch_view('test')
        shell._handle_key(' ')
        assert app.views['test'].presses == 1
    finally:
        os.unlink(f.name)


def test_help_toggle():
    """? toggles help overlay on and off."""
    shell, path = _make_shell()
    try:
        assert shell._help_active is False
        shell._help_view.build_content()
        assert shell._help_view.line_count > 10
        # Test state toggle directly (skip relayout — needs terminal)
        shell._help_active = True
        assert shell._help_active is True
        shell._help_active = False
        assert shell._help_active is False
    finally:
        os.unlink(path)


def test_escape_dismisses_help():
    """Escape closes the help overlay like every other non-scroll key."""
    shell, path = _make_shell()
    try:
        shell._help_active = True
        shell._handle_key(Keyboard.ESCAPE)
        assert shell._help_active is False
    finally:
        os.unlink(path)


def test_help_command():
    """:help handler sets help_active and builds content."""
    shell, path = _make_shell()
    try:
        assert shell._help_active is False
        shell._help_view.build_content()
        assert shell._help_view.line_count > 10
        # Verify help_active can be set (skip exec_command_inner,
        # which calls _relayout requiring a terminal)
        shell._help_active = True
        assert shell._help_active is True
    finally:
        os.unlink(path)


def test_help_scroll():
    """Scroll keys adjust offset within bounds."""
    shell, path = _make_shell()
    try:
        shell._help_active = True
        shell._help_view.build_content()
        total = shell._help_view.line_count
        shell._help_view.scroll(1, 10)
        assert shell._help_view._scroll == 1
        shell._help_view.scroll(-5, 10)
        assert shell._help_view._scroll == 0
        shell._help_view.scroll(total + 100, 10)
        assert shell._help_view._scroll <= total
    finally:
        os.unlink(path)


def test_key_hints_returned():
    """All non-splash views provide key_hints."""
    shell, path = _make_shell()
    try:
        for app in shell.apps.values():
            for name, view in app.views.items():
                hints = view.key_hints()
                assert isinstance(hints, dict)
    finally:
        os.unlink(path)


# ─── App/view navigation ─────────────────────────────────────────────────

class _NavView(View):
    def __init__(self, name):
        super().__init__()
        self.name = name


class _NavApp(App):
    def __init__(self, name, view_names, default_view):
        super().__init__()
        self.name = name
        self.default_view = default_view
        self.views = {n: _NavView(n) for n in view_names}


def _make_nav_shell():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    daemon = DummyDaemon(config)
    apps = [
        _NavApp('a', ['a1'], 'a1'),
        _NavApp('b', ['b1', 'b2'], 'b1'),
        _NavApp('c', ['c1'], 'c1'),
    ]
    shell = Shell(apps=apps, config=config, daemon=daemon)
    shell._relayout = lambda: None
    return shell, f.name


def test_tab_toggles_between_current_and_previous_app():
    """Tab is a last-app toggle, not a linear cycle."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        assert shell._active_app == 'b'
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'a'
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'b'
    finally:
        os.unlink(path)


def test_tab_without_previous_app_is_noop():
    """With no previous app, Tab must not cycle — it hints and stays put."""
    shell, path = _make_nav_shell()
    try:
        assert shell._prev_app == ''
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'a'
        assert shell._result_message
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_record_start_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    shell, path = _make_wifi_shell()
    try:
        shell._record_start('~/cap.pcap', False)
        assert (tmp_path / 'cap.pcap').exists()
        shell._record_stop()
    finally:
        os.unlink(path)


def test_unknown_theme_reports_problem():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('theme no_such_theme')
        assert shell._result_problem is True
        assert 'no_such_theme' in shell._result_message
    finally:
        os.unlink(path)


def test_bare_theme_requires_a_name():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('theme')
        assert shell._result_problem is True
        assert 'usage' in shell._result_message
    finally:
        os.unlink(path)


def test_known_theme_applies_silently():
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('theme tokyonight')
        assert shell._result_problem is False
        assert shell.style is STYLES['tokyonight']
    finally:
        os.unlink(path)


def test_command_result_separates_answers_from_problems():
    """Query answers are normal colour; refusals are problems."""
    shell, path = _make_shell()
    try:
        shell._exec_command_inner('fpga')
        assert shell._result_message.startswith('fpga:')
        assert shell._result_problem is False

        shell._exec_command_inner('fpga bogus')
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_tab_restores_previous_apps_last_view():
    """Returning via Tab lands on the view you left, not splash."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        shell.apps['b'].switch_view('b2')
        shell._exec_command_inner('a')
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'b'
        assert shell.active_app.active_view.name == 'b2'
    finally:
        os.unlink(path)


def test_explicit_app_switch_updates_previous():
    """A ':app' jump updates the Tab target, same as an app-level switch."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        shell._exec_command_inner('c')
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'b'
    finally:
        os.unlink(path)


def test_explicit_app_switch_restores_last_view():
    """:app with no view lands on that app's last view."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        shell.apps['b'].switch_view('b2')
        shell._exec_command_inner('a')
        shell._exec_command_inner('b')
        assert shell._active_app == 'b'
        assert shell.active_app.active_view.name == 'b2'
    finally:
        os.unlink(path)


def test_backtick_cycles_views_within_app():
    """Backtick advances through the current app's views and wraps."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        assert shell.active_app.active_view.name == 'b1'
        shell._handle_key('`')
        assert shell.active_app.active_view.name == 'b2'
        shell._handle_key('`')
        assert shell.active_app.active_view.name == 'b1'
    finally:
        os.unlink(path)


def test_backtick_leaves_app_and_previous_untouched():
    """Cycling views must not alter the Tab (app) target."""
    shell, path = _make_nav_shell()
    try:
        shell._exec_command_inner('b')
        shell._exec_command_inner('c')
        shell._handle_key('`')
        assert shell._active_app == 'c'
        shell._handle_key(Keyboard.TAB)
        assert shell._active_app == 'b'
    finally:
        os.unlink(path)


def test_keys_are_enqueued_and_run_on_drain(monkeypatch):
    shell, path = _make_shell()
    try:
        seen = []
        monkeypatch.setattr(shell, '_handle_key',
                            lambda key: seen.append(key))
        shell._enqueue_key('l')
        assert seen == []
        shell.on_actions()
        assert seen == ['l']
    finally:
        os.unlink(path)


def test_handle_key_refuses_off_thread_call():
    shell, path = _make_shell()
    try:
        errors = []

        def call():
            try:
                shell._handle_key('l')
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        worker.join()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
    finally:
        os.unlink(path)


def test_drain_routes_handler_errors_to_the_result_line():
    shell, path = _make_shell()
    try:
        def boom():
            raise ValueError('nope')

        shell._post(boom)
        shell.on_actions()
        assert 'nope' in shell._result_message
        assert shell._result_problem is True
    finally:
        os.unlink(path)


def test_daemon_swap_waits_for_the_ui_thread():
    shell, path = _make_shell()
    try:
        old = shell.daemon
        shell._exec_command_sync('connect dummy')
        assert shell.daemon is old
        shell.on_actions()
        assert shell.daemon is not old
        assert shell.apps['test'].views['test'].daemon is shell.daemon
    finally:
        os.unlink(path)


def test_install_daemon_refuses_off_thread_call():
    shell, path = _make_shell()
    try:
        errors = []

        def call():
            try:
                shell._install_daemon(DummyDaemon(shell.config))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        worker.join()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
    finally:
        os.unlink(path)


def test_background_problem_waits_for_the_ui_thread():
    shell, path = _make_shell()
    try:
        shell._exec_command_sync('daemon nope')
        assert shell._result_message == ''
        shell.on_actions()
        assert 'unknown subcommand' in shell._result_message
    finally:
        os.unlink(path)


class _FakeRemote(SheshaDaemon):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.stop_ok = True
        self.start_ok = True
        self.start_marks_fresh = True

    def sync(self):
        self.calls.append('sync')
        return True

    def stop(self):
        self.calls.append('stop')
        return self.stop_ok

    def start(self, port=None):
        self.calls.append(('start', port))
        if self.start_ok and self.start_marks_fresh:
            self._fresh_start = True
        return self.start_ok


def test_daemon_sync_installs_the_candidate(monkeypatch):
    shell, path = _make_shell()
    try:
        old = _FakeRemote(shell.config)
        shell.daemon = old
        candidate = _FakeRemote(shell.config)
        monkeypatch.setattr(shell, '_candidate_daemon',
                            lambda *a, **kw: candidate)

        shell._exec_command_sync('daemon sync', target_is_real=True)
        assert shell.daemon is old
        assert candidate.calls == ['sync']

        shell.on_actions()
        assert shell.daemon is candidate
    finally:
        os.unlink(path)


def test_daemon_stop_installs_the_candidate(monkeypatch):
    shell, path = _make_shell()
    try:
        old = _FakeRemote(shell.config)
        shell.daemon = old
        candidate = _FakeRemote(shell.config)
        monkeypatch.setattr(shell, '_candidate_daemon',
                            lambda *a, **kw: candidate)

        shell._exec_command_sync('daemon stop', target_is_real=True)
        shell.on_actions()
        assert candidate.calls == ['stop']
        assert shell.daemon is candidate
    finally:
        os.unlink(path)


def test_daemon_start_keeps_the_link_when_already_running(monkeypatch):
    shell, path = _make_shell()
    try:
        old = _FakeRemote(shell.config)
        shell.daemon = old
        candidate = _FakeRemote(shell.config)
        candidate.start_marks_fresh = False
        monkeypatch.setattr(shell, '_candidate_daemon',
                            lambda *a, **kw: candidate)

        shell._exec_command_sync('daemon start', target_is_real=True)
        shell.on_actions()
        assert candidate.calls == [('start', None)]
        assert shell.daemon is old
    finally:
        os.unlink(path)


def test_daemon_start_installs_a_freshly_launched_daemon(monkeypatch):
    shell, path = _make_shell()
    try:
        old = _FakeRemote(shell.config)
        shell.daemon = old
        candidate = _FakeRemote(shell.config)
        monkeypatch.setattr(shell, '_candidate_daemon',
                            lambda *a, **kw: candidate)

        shell._exec_command_sync('daemon start', target_is_real=True)
        shell.on_actions()
        assert shell.daemon is candidate
    finally:
        os.unlink(path)


def test_install_applies_the_active_view_mode():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        shell = Shell(apps=[cls() for cls in APP_CLASSES], config=config,
                      daemon=DummyDaemon(config))

        shell._exec_command_inner('fm')
        assert shell.daemon.get_state().get('mode') == '3'

        shell._exec_command_sync('connect dummy')
        shell.on_actions()

        assert shell.daemon.get_state().get('mode') == '3'
        assert any(fr['type'] == 'fm' for fr in shell.daemon.poll())
    finally:
        os.unlink(f.name)


def test_lifecycle_rejects_a_second_operation():
    shell, path = _make_shell()
    try:
        shell._lifecycle_active = 'connect'
        shell._run_daemon(['sync'])
        assert 'in progress' in shell._result_message
        assert shell._lifecycle_active == 'connect'
    finally:
        os.unlink(path)


def test_lifecycle_worker_error_is_posted():
    shell, path = _make_shell()
    try:
        def boom():
            raise RuntimeError('nope')

        shell._lifecycle_active = 'connect'
        shell._run_lifecycle(boom)
        assert shell._result_message == ''
        shell.on_actions()
        assert 'nope' in shell._result_message
        assert shell._lifecycle_active is None
    finally:
        os.unlink(path)


def test_pcap_write_survives_a_closed_file():
    """A stop racing a write must not escape as an exception."""
    shell, path = _make_shell()
    try:
        class _ClosedFile:
            def write(self, data):
                raise ValueError('I/O operation on closed file')

            def close(self):
                pass

        shell._recording = True
        shell._rec_file = _ClosedFile()
        shell._rec_eapol = False
        shell._rec_frames = 0
        shell._relayout = lambda: None
        shell._write_pcap_frame({
            'type': 'wifi', 'psdu': b'\x00', 'fcs_ok': True,
            'rx_gain': 0.0, 'rate_mbps': 1,
        })
        assert shell._recording is False
    finally:
        os.unlink(path)


def test_freq_pulse_settles_when_response_arrives_before_drain():
    """The status reply may reach the queue before the next UI frame.

    Starting the pulse and settling it must happen in queue order; a reply
    processed too early must not leave the field throbbing forever.
    """
    shell, path = _make_shell()
    try:
        sender = threading.Thread(target=lambda: shell.daemon._sent('freq'))
        sender.start()
        sender.join()

        shell.daemon._notify('freq')
        shell.daemon.drain_notifications()

        assert shell._animator.running('status_freq')
        shell._animator.tick(2.0)
        shell._animator.tick(1.0)
        assert not shell._animator.running('status_freq')
    finally:
        os.unlink(path)


def test_command_line_marks_cursor():
    from shesha.tui.pytui import Text
    from shesha.tui.shell import command_line

    line = command_line('wifi', 2, {'input_active': 0xffffff})
    assert Text(line).strip_ansi() == ':wifi'
    assert '\x1b[7m' in line


def test_status_notification_drives_gain_heat(monkeypatch):
    shell, path = _make_shell()
    try:
        state = {'clip_count': '0', 'sample_count': '0'}
        monkeypatch.setattr(shell.daemon, 'get_state', lambda: dict(state))
        shell._on_daemon_change('_status')
        assert not shell._animator.running('gain_heat')

        state.update(clip_count='1000', sample_count='10000')
        shell._on_daemon_change('_status')
        assert shell._animator.running('gain_heat')
        shell._animator.tick(0.5)
        assert shell._animator.get('gain_heat') > 0.9
    finally:
        os.unlink(path)


# ─── Command tree: view persistence, shortcuts, recording, fallback ──────


def _make_app_shell(*apps):
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    config = Config(f.name)
    shell = Shell(apps=list(apps), config=config,
                  daemon=DummyDaemon(config))
    return shell, f.name


def test_switching_view_saves_app_view():
    """Entering an app at its restored view must still persist app.view."""
    from shesha.tui.fm import FmApp
    from shesha.tui.wifi import WifiApp

    shell, path = _make_app_shell(WifiApp(), FmApp())
    try:
        shell._exec_command_inner('fm rx')
        assert shell.config.get('app.view') == 'fm:rx'
    finally:
        os.unlink(path)


def test_bare_gain_shortcut_requires_a_value():
    shell, path = _make_shell()
    try:
        shell._last_manual_gain = '37'
        shell._exec_command_inner('gain')
        assert shell._result_problem is True
        assert 'usage' in shell._result_message
        assert shell._last_manual_gain == '37'
    finally:
        os.unlink(path)


def test_bare_freq_shortcut_requires_a_value():
    shell, path = _make_shell()
    try:
        shell._last_manual_gain = '42'
        shell._exec_command_inner('freq')
        assert shell._result_problem is True
        assert 'usage' in shell._result_message
        assert shell._last_manual_gain == '42'
    finally:
        os.unlink(path)


def _make_wifi_shell():
    from shesha.tui.wifi import WifiApp
    return _make_app_shell(WifiApp())


def test_wifi_record_start_then_stop():
    shell, path = _make_wifi_shell()
    started, stopped = [], []
    try:
        shell._record_start = lambda filename, eapol_only: started.append(
            (filename, eapol_only))
        shell._record_stop = lambda: stopped.append(True)
        shell._exec_command_inner('wifi record capture.pcap')
        assert started == [('capture.pcap', False)]
        shell._exec_command_inner('wifi record stop')
        assert stopped == [True]
    finally:
        os.unlink(path)


def test_wifi_record_keeps_spaces_in_the_path():
    shell, path = _make_wifi_shell()
    started = []
    try:
        shell._record_start = lambda filename, eapol_only: started.append(
            (filename, eapol_only))
        shell._exec_command_inner('wifi record my capture.pcap')
        assert started == [('my capture.pcap', False)]
    finally:
        os.unlink(path)


def test_active_app_record_resolves_without_prefix():
    shell, path = _make_wifi_shell()
    started, stopped = [], []
    try:
        shell._record_start = lambda filename, eapol_only: started.append(
            (filename, eapol_only))
        shell._record_stop = lambda: stopped.append(True)
        shell._exec_command_inner('record capture.pcap')
        assert started == [('capture.pcap', False)]
        shell._exec_command_inner('wifi record stop')
        assert stopped == [True]
    finally:
        os.unlink(path)


def test_fm_rx_free_form_command_tunes():
    """An unmatched token falls through to the active view's command()."""
    from shesha.tui.fm import FmApp
    from shesha.tui.wifi import WifiApp

    shell, path = _make_app_shell(WifiApp(), FmApp())
    try:
        shell._exec_command_inner('fm rx')
        shell._exec_command_inner('88.5M')
        assert shell.config.get('freq') == '88500000'
        assert shell.daemon.get_state().get('freq') == '88500000'
    finally:
        os.unlink(path)


def test_unknown_wifi_subcommand_reports_problem():
    """A bad argument on an app entry must reach the result line."""
    shell, path = _make_wifi_shell()
    try:
        shell._exec_command_inner('wifi bogus extra')
        assert shell._result_problem is True
        assert 'bogus' in shell._result_message
    finally:
        os.unlink(path)


def test_fm_tx_missing_file_reports_problem(tmp_path):
    """:fm tx <path> with no such file must reach the result line."""
    from shesha.tui.fm import FmApp
    shell, path = _make_app_shell(FmApp())
    try:
        shell._exec_command_inner(f'fm tx {tmp_path}/nope.mp3')
        assert shell._result_problem is True
        assert 'nope.mp3' in shell._result_message
    finally:
        os.unlink(path)


def test_bad_free_form_argument_reports_problem():
    """The active-view fallback path must also report bad arguments."""
    from shesha.tui.fm import FmApp
    shell, path = _make_app_shell(FmApp())
    try:
        shell._exec_command_inner('fm rx')
        shell._exec_command_inner('not-a-frequency')
        assert shell._result_problem is True
    finally:
        os.unlink(path)
