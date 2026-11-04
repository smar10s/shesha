"""Tests for the waterfall view's keybind config handling."""
import json
import os
import tempfile

from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.viz import VizApp, WaterfallView


def _view():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    added = VizApp.config_keys()
    Config.register_keys(added)
    config = Config(f.name)
    view = WaterfallView()
    view.config = config
    view.daemon = DummyDaemon(config)
    return view, config, f.name, added


def _cleanup(path, added):
    for key in added:
        Config._schema.pop(key, None)
    os.unlink(path)


def test_fft_keybind_advances_cycle_base():
    """Pressing 's' repeatedly must walk the size list, not stick.

    Regression: the keybind advanced the stored config but not the view's own
    _fft_size, so every press recomputed the same "next" from a stale base.
    """
    view, config, path, added = _view()
    try:
        view._fft_size = 1024
        view.keybinds()['s'](1)
        assert view._fft_size == 2048
        view.keybinds()['s'](1)
        assert view._fft_size == 4096
        assert config.get('viz.waterfall.fft_size') == '4096'
        assert view.daemon.get_state()['fft_size'] == '4096'
    finally:
        _cleanup(path, added)


def test_avg_keybind_updates_running_mode():
    """Pressing 'a' must change what is displayed, not just the stored value."""
    view, config, path, added = _view()
    try:
        view._avg_mode = 'off'
        view.keybinds()['a'](1)
        assert view._avg_mode == 'window'
        assert config.get('viz.waterfall.avg_mode') == 'window'
    finally:
        _cleanup(path, added)


def test_frame_buffer_does_not_grow_before_layout():
    view, config, path, added = _view()
    try:
        for i in range(1000):
            view.update({'bins': [0.0] * 8, 'ts': float(i)})
        assert view._frame_buffer == []
    finally:
        _cleanup(path, added)


def test_frame_buffer_is_capped_while_waiting_to_render():
    from shesha.tui.pytui import StyledWindow

    view, config, path, added = _view()
    try:
        view.layout(StyledWindow(0, 0, 80, 24, {}))
        view._clock = lambda: 0.0
        view._last_row_time = 0.0
        for i in range(1000):
            view.update({'bins': [0.0] * 8, 'ts': float(i)})
        assert len(view._frame_buffer) <= 256
    finally:
        _cleanup(path, added)


def test_range_commands_keep_a_usable_span():
    """Equal floor and ceiling must be refused, not divided by zero."""
    view, config, path, added = _view()
    try:
        assert view.command(['floor', '-40'])[0] is None
        error, _ = view.command(['ceil', '-40'])
        assert error is not None
        assert view._db_ceil - view._db_floor >= 10
        assert view.config.get('viz.waterfall.ceil') == '-20.0'
    finally:
        _cleanup(path, added)


def test_config_updates_keep_a_usable_span():
    """A hand-edited config must not be able to collapse the range."""
    view, config, path, added = _view()
    try:
        view.config_updated('viz.waterfall.floor', '-20')
        view.config_updated('viz.waterfall.ceil', '-20')
        assert view._db_ceil - view._db_floor >= 10
    finally:
        _cleanup(path, added)


def test_freq_axis_stays_unknown_until_daemon_reports_it():
    from shesha.tui.pytui import StyledWindow, Text

    view, config, path, added = _view()
    try:
        view.layout(StyledWindow(0, 0, 80, 24, {}))
        line = Text(view._freq_win.buffer[0]).strip_ansi().strip()
        assert line == '--'

        class D:
            def get_state(self):
                return {'freq': '2457000000', 'sample_rate': '20000000'}

        view.config_changed('freq', D())
        view.config_changed('sample_rate', D())
        line = Text(view._freq_win.buffer[0]).strip_ansi()
        assert '2.457 GHz' in line
    finally:
        _cleanup(path, added)


def test_colour_mapping_survives_a_degenerate_range():
    """Defence in depth: a collapsed range must clamp, not raise."""
    view, config, path, added = _view()
    try:
        view._db_floor = view._db_ceil = -20.0
        assert view._db_to_color(-20.0) in view._cmap
    finally:
        _cleanup(path, added)
