"""Tests for the `iq rx` sidecar metadata (`--meta`).

The capture settings were not recorded in the earlier session's /tmp files,
which made the on-air CNR delta impossible to attribute after the fact.
`iq rx --meta` writes them next to the capture so a capture is self-describing.
"""
import json

from shesha.cli.iq import build_iq_meta, write_iq_meta
from shesha.protocol import Status, RadioPolicy


def _status(**kw):
    base = dict(
        freq_hz=90_100_000, sample_rate_hz=2_100_000, bw_hz=200_000,
        fft_size=4096, gain_db=35.0, agc_mode=0, window_type=1,
        target_fps=50, daemon_mode=3, tx_atten_db=10.0, tx_busy=0,
        tx_proto=0, tx_flags=0, tx_repeat=0, tx_interval_us=0,
        fpga_active=False, rx_active=True, iq_bits=12,
        psd_coverage=1.0, workers=1,
        radio=RadioPolicy(0, 0, 200_000, 0, 0, 2_100_000, False, 0),
        clip_count=0, sample_count=0, accum=1, fpga_running=False,
    )
    base.update(kw)
    return Status(**base)


def test_build_iq_meta_carries_settings_and_capture_stats():
    meta = build_iq_meta(
        _status(), out="/tmp/x.f32q", bits=12, n_samples=1000,
        n_bytes=3000, duration_s=5.0, dropped_frames=0, timestamp=1234.5,
    )
    assert meta["freq_hz"] == 90_100_000
    assert meta["sample_rate_hz"] == 2_100_000
    assert meta["bw_hz"] == 200_000
    assert meta["gain_db"] == 35.0
    assert meta["agc_mode"] == 0
    assert meta["iq_bits"] == 12
    assert meta["n_samples"] == 1000
    assert meta["n_bytes"] == 3000
    assert meta["duration_s"] == 5.0
    assert meta["dropped_frames"] == 0
    assert meta["out"] == "/tmp/x.f32q"
    assert meta["timestamp"] == 1234.5


def test_write_iq_meta_is_a_sidecar(tmp_path):
    cap = tmp_path / "cap.f32q"
    path = write_iq_meta(str(cap), {"gain_db": 35.0})
    assert path == str(cap) + ".meta.json"
    assert json.load(open(path))["gain_db"] == 35.0
