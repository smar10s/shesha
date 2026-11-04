"""Tests for App/View base classes — instance-vs-shared mutable state."""
import json
import os
import tempfile

import pytest

from shesha.tui.app import App, View
from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.wifi import WifiApp
from shesha.tui.fm import FmApp


def _noop(_args: list[str], _daemon) -> tuple:
    return (None, '')


def test_app_commands_are_instance_isolated():
    wifi = WifiApp()
    assert set(wifi.commands) >= {'tx', 'record'}
    fm = FmApp()
    assert set(fm.commands) == {'rds'}
    assert set(fm.views) == {'rx', 'tx'}


def test_app_commands_construct_order_independent():
    fm = FmApp()
    wifi = WifiApp()
    assert set(fm.commands) == {'rds'}
    assert set(wifi.commands) >= {'tx', 'record'}
    assert 'rx' in fm.views
    assert 'tx' in fm.views and 'tx' not in fm.commands


def test_app_commands_dict_is_per_instance():
    a = App()
    b = App()
    a.commands['foo'] = _noop
    assert b.commands == {}
    assert 'foo' in a.commands


def test_validate_rejects_view_command_name_overlap():
    class ClashApp(App):
        name = 'clash'
        default_view = 'main'

        def __init__(self):
            super().__init__()
            self.views = {'main': View(), 'tx': View()}
            self.commands['tx'] = _noop

    with pytest.raises(ValueError, match='overlap'):
        ClashApp().validate()


def test_view_apply_config_forwards_radio_keys_to_daemon():
    """View-internal radio changes must reach the daemon.

    Regression: views used to rely on the Config observer to deliver
    radio keys (wifi scan hop, fm tune, viz fft/fps).  Config is
    write-only now — views must forward through the daemon explicitly.
    """
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        daemon = DummyDaemon(config)
        view = View()
        view.config = config
        view.daemon = daemon

        view.apply_config('freq', '5160000000')
        assert config.get('freq') == '5160000000'
        assert daemon.get_state()['freq'] == '5160000000'

        # View-local keys persist without touching the daemon
        view.apply_config('theme', 'cyberpunk')
        assert config.get('theme') == 'cyberpunk'
    finally:
        os.unlink(f.name)


def test_view_apply_config_forwards_normalized_symbolic_value():
    """Symbolic settings must reach the daemon as their wire numbers.

    Regression: the view forwarded the typed text, so window 'hann' hit the
    daemon's int() and raised.
    """
    from shesha.tui.viz import VizApp
    added = VizApp.config_keys()
    Config.register_keys(added)
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        config = Config(f.name)
        daemon = DummyDaemon(config)
        view = View()
        view.config = config
        view.daemon = daemon

        view.apply_config('viz.waterfall.window', 'hann')
        assert config.get('viz.waterfall.window') == '1'
        assert daemon.get_state()['window'] == '1'
    finally:
        for key in added:
            Config._schema.pop(key, None)
        os.unlink(f.name)