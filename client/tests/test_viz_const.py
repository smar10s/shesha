"""Constellation view: decoding, heatmap accumulation, config."""
import itertools
import json
import os
import tempfile

from shesha.protocol import pack_iq
from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.pytui import StyledWindow, Text
from shesha.tui.viz import VizApp, ConstView


class _SpyDaemon(DummyDaemon):
    """Records set_iq_bits calls; DummyDaemon gains that method in Task 6."""

    def __init__(self, config):
        super().__init__(config)
        self.iq_bits_calls = []
        self.accept_bits = True

    def set_iq_bits(self, bits: int) -> bool:
        self.iq_bits_calls.append(bits)
        if not self.accept_bits:
            return False
        return super().set_iq_bits(bits)


def _bar_text(view) -> str:
    return Text(view._bar_win.buffer[0]).strip_ansi()


def _view():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    added = VizApp.config_keys()
    Config.register_keys(added)
    config = Config(f.name)
    view = ConstView()
    view.config = config
    view.daemon = _SpyDaemon(config)
    view.layout(StyledWindow(0, 0, 40, 12, {}))
    return view, config, f.name, added


def _cleanup(path, added):
    for key in added:
        Config._schema.pop(key, None)
    os.unlink(path)


def _iq(bits, pts):
    return {'type': 'iq', 'ts': 0.0, 'bits': bits, 'n_samples': len(pts),
            'samples': pack_iq(pts, bits), 'sample_rate_hz': 2_100_000,
            'freq_mhz': 915.0}


def test_const_view_is_iq_mode():
    assert ConstView.mode == 2


def test_const_ignores_non_iq_frames():
    view, config, path, added = _view()
    try:
        view.update({'type': 'psd', 'bins': [1.0, 2.0]})
        assert not any(view._heat)
    finally:
        _cleanup(path, added)


def test_const_accumulates_heat_from_iq_frame():
    view, config, path, added = _view()
    try:
        view.update(_iq(8, [complex(0.5, 0.5)] * 64))
        assert sum(view._heat) > 0.0
    finally:
        _cleanup(path, added)


def test_const_on_enter_pushes_configured_bits():
    view, config, path, added = _view()
    try:
        view.on_enter()
        assert view.daemon.iq_bits_calls == [8]
    finally:
        _cleanup(path, added)


def test_const_bits_push_suppressed_when_inactive():
    view, config, path, added = _view()
    try:
        assert view.active is False
        view.config_updated('viz.const.bits', '12')
        assert view.daemon.iq_bits_calls == []
        assert view._bits == 12
        view.active = True
        view.config_updated('viz.const.bits', '8')
        assert view.daemon.iq_bits_calls == [8]
    finally:
        _cleanup(path, added)


def test_const_bits_label_shows_the_wire_width():
    """The label must report what arrived, not what was requested."""
    view, config, path, added = _view()
    try:
        view.active = True
        view.config_updated('viz.const.bits', '12')
        view.update(_iq(8, [complex(0.5, 0.5)] * 64))
        label = _bar_text(view)
        assert '8-bit' in label
        assert '12-bit' not in label
    finally:
        _cleanup(path, added)


def test_const_refused_bits_push_keeps_previous_width():
    view, config, path, added = _view()
    try:
        view.active = True
        view.daemon.accept_bits = False
        view.config_updated('viz.const.bits', '12')
        assert view._bits == 8
    finally:
        _cleanup(path, added)


def test_const_range_keybind_zooms_and_resets():
    view, config, path, added = _view()
    try:
        assert not view._range  # auto
        view.keybinds()['='](1)
        assert view._range
        assert 'r' not in view.keybinds()   # global recording owns 'r'
        view.keybinds()['a'](1)
        assert view._range is None
        view.keybinds()['a'](1)
        assert view._range is not None      # auto -> lock is a toggle
    finally:
        _cleanup(path, added)


def test_const_range_keybind_updates_the_bar_without_a_frame():
    view, config, path, added = _view()
    try:
        view.keybinds()['a'](1)
        assert '(lock)' in _bar_text(view)
        view.keybinds()['a'](1)
        assert '(auto)' in _bar_text(view)
    finally:
        _cleanup(path, added)


def test_const_keeps_square_data_plane():
    """Equal I/Q scale: the heat grid is square, centered in the pane."""
    view, config, path, added = _view()   # 40x12 pane -> 40x22 pixels
    try:
        assert view._w == view._h == 22
        assert len(view._heat) == view._w * view._h
        assert view._ox == 9 and view._oy == 0   # pad the wide axis
    finally:
        _cleanup(path, added)


def test_const_pads_the_longer_axis():
    """A tall pane pads top/bottom instead of stretching the ring."""
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    added = VizApp.config_keys()
    Config.register_keys(added)
    try:
        config = Config(f.name)
        tall = ConstView()
        tall.config = config
        tall.layout(StyledWindow(0, 0, 20, 40, {}))   # 20x78 pixels -> side 20
        assert tall._w == tall._h == 20
        assert tall._ox == 0 and tall._oy == (78 - 20) // 2
    finally:
        for key in added:
            Config._schema.pop(key, None)
        os.unlink(f.name)


def test_const_rate_limits_processing():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    added = VizApp.config_keys()
    Config.register_keys(added)
    try:
        config = Config(f.name)
        # Virtual clock: 1 ms per update() call.
        ticks = itertools.count(0.0, 0.001)
        view = ConstView(clock=lambda: next(ticks))
        view.config = config
        view.daemon = _SpyDaemon(config)
        view.layout(StyledWindow(0, 0, 40, 12, {}))
        view.config_updated('viz.const.fps', '30')
        frame = _iq(8, [complex(0.5, 0.5)] * 256)
        for _ in range(100):
            view.update(frame)
        # 100 frames span 0.1 s of virtual time; at 30 fps ~3 process.
        assert 1 <= view._frames_processed <= 10
    finally:
        for key in added:
            Config._schema.pop(key, None)
        os.unlink(f.name)


def test_dummy_iq_mode_emits_iq_frames():
    from shesha.tui.dummy import DummyDaemon
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({}, f)
    f.close()
    try:
        daemon = DummyDaemon(Config(f.name))
        daemon.set_mode(2)
        frames = daemon.poll()
        assert frames and frames[0]['type'] == 'iq'
        assert frames[0]['bits'] == 8
    finally:
        os.unlink(f.name)
