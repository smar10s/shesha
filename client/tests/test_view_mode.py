"""Daemon mode is owned by the view and applied by the Shell on switch."""
import json
import os
import tempfile

from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.shell import Shell
from shesha.tui.app import App, View
from shesha.tui.status import SplashView
from shesha.tui.viz import WaterfallView
from shesha.tui.wifi import ScanView, TrafficView
from shesha.tui.fm import FmRxView, FmTxView
from shesha.protocol import DaemonMode


def test_base_view_mode_is_none():
    assert View().mode is None


def test_views_declare_their_daemon_mode():
    assert SplashView.mode is DaemonMode.WATERFALL
    assert WaterfallView.mode is DaemonMode.WATERFALL
    assert ScanView.mode is DaemonMode.WIFI
    assert TrafficView.mode is DaemonMode.WIFI
    assert FmRxView.mode is DaemonMode.FM
    assert FmTxView.mode is DaemonMode.FM


class _ModedView(View):
    name = 'moded'
    mode = DaemonMode.IQ


class _ModedApp(App):
    name = 'moded'
    default_view = 'moded'

    def __init__(self):
        super().__init__()
        self.views = {'moded': _ModedView()}


class _PlainView(View):
    name = 'plain'


class _PlainApp(App):
    name = 'plain'
    default_view = 'plain'

    def __init__(self):
        super().__init__()
        self.views = {'plain': _PlainView()}


def test_switching_views_applies_and_clears_mode():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        daemon = DummyDaemon(config)
        shell = Shell(apps=[_PlainApp(), _ModedApp()], config=config,
                      daemon=daemon)
        shell._exec_command_inner('moded')
        assert daemon.get_state()['mode'] == '2'
        # A view with mode None must leave the radio's mode untouched.
        shell._exec_command_inner('plain')
        assert daemon.get_state()['mode'] == '2'
    finally:
        os.unlink(f.name)


def test_app_switch_clears_previous_view_active():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        daemon = DummyDaemon(config)
        shell = Shell(apps=[_PlainApp(), _ModedApp()], config=config,
                      daemon=daemon)
        shell._exec_command_inner('moded')
        assert shell.apps['moded'].active_view.active is True
        shell._exec_command_inner('plain')
        assert shell.apps['moded'].active_view.active is False
        assert shell.apps['plain'].active_view.active is True
    finally:
        os.unlink(f.name)
