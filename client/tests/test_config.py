"""Tests for the write-only config system."""
import json
import os
import tempfile
from shesha.tui.config import Config


def test_set_get():
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({}, f)
        path = f.name
    try:
        c = Config(path)
        c.set('freq', '5180000000')
        assert c.get('freq') == '5180000000'
        assert c.get('missing', 'default') == 'default'
    finally:
        os.unlink(path)


def test_persistence():
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({'fps': '60'}, f)
        path = f.name
    try:
        c = Config(path)
        assert c.get('fps') == '60'
        c.set('fps', '30')
        c2 = Config(path)
        assert c2.get('fps') == '30'
    finally:
        os.unlink(path)


def test_confirmed_key_not_persisted_on_set():
    """Confirmed keys (radio params) only persist via persist(), not set()."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({'gain': '30'}, f)
        path = f.name
    try:
        c = Config(path)
        c.set('gain', '50')
        # In-memory value updated
        assert c.get('gain') == '50'
        # But disk still has old value
        c2 = Config(path)
        assert c2.get('gain') == '30'
        # persist() writes to disk
        c.persist('gain', '50')
        c3 = Config(path)
        assert c3.get('gain') == '50'
    finally:
        os.unlink(path)


def test_get_int_float():
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({'freq': '5180000000', 'bad': 'xyz'}, f)
        path = f.name
    try:
        c = Config(path)
        assert c.get_int('freq') == 5180000000
        assert c.get_float('freq') == 5180000000.0
        assert c.get_int('bad', 0) == 0
        assert c.get_float('bad', 0.0) == 0.0
    finally:
        os.unlink(path)


def test_freq_range_rejection(tmp_path):
    import pytest
    c = Config(str(tmp_path / 'cfg.json'))
    with pytest.raises(ValueError):
        c.set('freq', '50000000')      # 50 MHz — below 70 MHz min
    with pytest.raises(ValueError):
        c.set('freq', '7000000000')    # 7 GHz — above 6 GHz max


def test_bw_range_rejection(tmp_path):
    import pytest
    c = Config(str(tmp_path / 'cfg.json'))
    with pytest.raises(ValueError):
        c.set('bw', '100000')          # 100 kHz — below 200 kHz min
    with pytest.raises(ValueError):
        c.set('bw', '57000000')        # 57 MHz — above 56 MHz max


def test_sample_rate_range_rejection(tmp_path):
    import pytest
    c = Config(str(tmp_path / 'cfg.json'))
    with pytest.raises(ValueError):
        c.set('sample_rate', '500000')     # 500 kHz — below 520833 min
    with pytest.raises(ValueError):
        c.set('sample_rate', '62000000')   # 62 MHz — above 61.44 MHz max


def test_confirmed_keys_are_the_hardware_radio_set():
    assert set(Config.confirmed_keys()) == {
        'freq', 'gain', 'tx_gain', 'bw', 'sample_rate'}


def test_get_normalized_converts_symbolic_defaults(tmp_path):
    """Symbolic defaults must reach the daemon in wire form, not as text."""
    from shesha.tui.viz import VizApp
    added = VizApp.config_keys()
    Config.register_keys(added)
    try:
        c = Config(str(tmp_path / 'cfg.json'))
        assert c.get_normalized('viz.waterfall.window') == '1'    # 'hann'
        assert c.get_normalized('viz.waterfall.accum') == '1'     # 'max'
        assert c.get_normalized('viz.waterfall.fft_size') == '4096'
        assert c.get_normalized('unknown.key') is None
    finally:
        for key in added:
            Config._schema.pop(key, None)


def test_bool_spec_accepts_common_spellings():
    from shesha.tui.config import KeySpec
    spec = KeySpec('bool', default=False)
    for v in ('true', 'false', '1', '0', 'yes', 'no', 'on', 'off',
              True, False, 'TRUE', 'Off'):
        ok, err = spec.validate(v)
        assert ok, (v, err)
    assert spec.normalize('TRUE') == 'true'
    assert spec.normalize('Off') == 'false'
    assert spec.normalize(True) == 'true'
    ok, _ = spec.validate('maybe')
    assert not ok


def test_bool_config_roundtrip(tmp_path):
    from shesha.tui.config import KeySpec
    Config.register_keys({'test.flag': KeySpec('bool', default=False)})
    try:
        c = Config(str(tmp_path / 'cfg.json'))
        assert c.get_bool('test.flag') is False
        c.set('test.flag', 'true')
        assert c.get_bool('test.flag') is True
        assert Config(str(tmp_path / 'cfg.json')).get_bool('test.flag') is True
        c.set('test.flag', 'false')
        assert c.get_bool('test.flag') is False
    finally:
        Config._schema.pop('test.flag', None)
