"""Tests for the daemon abstraction layer."""
import json
import os
import subprocess
import sys
import tempfile
from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon


def test_dummy_import_does_not_require_scapy():
    """The offline daemon must not pull in an undeclared dependency."""
    code = "import sys; sys.modules['scapy'] = None; import shesha.tui.dummy"
    subprocess.run([sys.executable, "-c", code], check=True)


def _make_config():
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump({'freq': '5180000000', 'gain': '30', 'bw': '20000000'}, f)
    f.close()
    return Config(f.name), f.name


def test_dummy_initial_state():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        state = d.get_state()
        assert state['freq'] == '5180000000'
        assert d.connected is True
        assert d.busy is False
    finally:
        os.unlink(path)


def test_dummy_apply_sets_state():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        changes = []
        d.on_change(lambda k: changes.append(k))
        d.apply('gain', '50')
        # State updates immediately (dummy IS the daemon)
        assert d.get_state()['gain'] == '50'
        assert d.get_state()['agc_mode'] == 'manual'
        # Transient send feedback + confirmed change both notified
        assert '_sent' in changes
        assert 'gain' in changes
    finally:
        os.unlink(path)


def test_dummy_apply_auto_gain():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        d.apply('gain', 'auto')
        assert d.get_state()['gain'] == 'auto'
        assert d.get_state()['agc_mode'] == 'fast'
        d.apply('gain', 'slow')
        assert d.get_state()['agc_mode'] == 'slow'
    finally:
        os.unlink(path)


def test_dummy_poll_returns_psd():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        frames = d.poll()
        assert len(frames) == 1
        assert 'bins' in frames[0]
        assert len(frames[0]['bins']) == 1024
    finally:
        os.unlink(path)


def test_dummy_apply_config_maps_radio_keys():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        changes = []
        d.on_change(lambda k: changes.append(k))
        d.apply_config('gain', '45')
        assert d.get_state()['gain'] == '45'
        assert '_sent' in changes
    finally:
        os.unlink(path)


def test_radio_config_keys_derived_from_radio_map():
    """The seed set is derived from the radio map, not hard-coded."""
    from shesha.tui.daemon import Daemon
    keys = Daemon.radio_config_keys()
    assert 'fps' in keys
    assert 'sample_rate' in keys
    assert 'agc' not in keys  # gain is the single AGC knob


def test_dummy_seeds_fps_from_config():
    """fps is no longer a straggler: the dummy seeds target_fps from config."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({'fps': '42'}, f)
        path = f.name
    try:
        d = DummyDaemon(Config(path))
        assert d.get_state()['target_fps'] == '42'
    finally:
        os.unlink(path)


def test_dummy_set_mode_updates_state():
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        changes = []
        d.on_change(lambda k: changes.append(k))
        d.set_mode(1)
        assert d.get_state()['mode'] == '1'
        assert 'mode' in changes
    finally:
        os.unlink(path)


# ─── FM TX streaming tests ──────────────────────────────────────────────


def test_fm_tx_stream_start():
    """Starting an FM TX stream marks tx_busy and returns True."""
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        ok = d.fm_tx_stream_start([0.5] * 100)
        assert ok is True
        assert d.get_state()['tx_busy'] == '1'
    finally:
        os.unlink(path)


def test_fm_tx_stream_push():
    """Pushing audio into an active stream returns OK."""
    from shesha.client import PushResult
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        d.fm_tx_stream_start([0.5] * 100)
        result = d.fm_tx_stream_push([0.3] * 100)
        assert result == PushResult.OK
    finally:
        os.unlink(path)


def test_fm_tx_stream_end():
    """Ending a stream clears tx_busy and returns True."""
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        d.fm_tx_stream_start([0.5] * 100)
        ok = d.fm_tx_stream_end()
        assert ok is True
        assert d.get_state()['tx_busy'] == '0'
    finally:
        os.unlink(path)


def test_fm_tx_stats():
    """tx_stats returns a TxStats with correct idle/active state."""
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        stats = d.tx_stats()
        assert stats is not None
        assert stats.tx_active is False

        d.fm_tx_stream_start([0.5] * 100)
        stats = d.tx_stats()
        assert stats.tx_active is True
        assert stats.fm_active is True
    finally:
        os.unlink(path)


def test_dummy_fm_tx_stats_advance_with_playout():
    """Offline TX telemetry must move, or the visualiser stays blank."""
    config, path = _make_config()
    try:
        d = DummyDaemon(config)
        now = [100.0]
        d._clock = lambda: now[0]
        d.fm_tx_stream_start([0.5] * 44100, audio_rate=44100,
                             dac_rate=44100)
        assert d.tx_stats().emitted == 0

        now[0] += 0.5
        assert d.tx_stats().emitted == 22050

        d.fm_tx_stream_push([0.5] * 44100)
        now[0] += 0.5
        assert d.tx_stats().emitted == 44100

        d.fm_tx_stream_end()
        assert d.tx_stats().tx_active is False
    finally:
        os.unlink(path)
