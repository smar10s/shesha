"""Human output is the default; JSON is opt-in."""
from shesha.cli.status import format_caps, format_status
from shesha.cli.wifi import format_tx_status
from shesha.protocol import Capabilities, DaemonMode, RadioPolicy, Status


def _status(**kw):
    base = dict(
        freq_hz=100_100_000, sample_rate_hz=61_440_000, bw_hz=20_000_000,
        fft_size=4096, gain_db=35.0, agc_mode=0, window_type=1,
        target_fps=20, daemon_mode=DaemonMode.WATERFALL, tx_atten_db=10.0,
        tx_busy=0, tx_proto=0, tx_flags=0, tx_repeat=0, tx_interval_us=0,
        fpga_active=False, rx_active=True, iq_bits=12, psd_coverage=1.0,
        workers=1,
        radio=RadioPolicy(0, 0, 20_000_000, 0, 0, 61_440_000, False, 0),
        clip_count=0, sample_count=0, accum=1, fpga_running=False,
    )
    base.update(kw)
    return Status(**base)


def test_format_status_has_core_fields():
    text = format_status(_status(), DaemonMode.WATERFALL)
    assert "mode:       waterfall" in text
    assert "frequency:  100.100 MHz" in text
    assert "bandwidth:  20.000 MHz" in text
    assert "gain:       35.0 dB (manual)" in text
    assert "fft:        4096 bins (hann), 20 fps" in text
    assert "tx:         idle, atten 10.0 dB" in text
    assert "iq:         12-bit" in text
    assert "psd:        1 worker, 100.0% coverage" in text


def test_format_status_shows_busy_tx_and_psd_load():
    text = format_status(
        _status(tx_busy=1, tx_proto=0, tx_atten_db=12.5,
                iq_bits=8, psd_coverage=0.75, workers=2),
        DaemonMode.WIFI)
    assert "tx:         busy (wifi), atten 12.5 dB" in text
    assert "iq:         8-bit" in text
    assert "psd:        2 workers, 75.0% coverage" in text


def test_format_status_shows_iq_tx():
    text = format_status(
        _status(tx_busy=1, tx_proto=2), DaemonMode.IQ)
    assert "tx:         busy (iq)" in text


def test_format_status_distinguishes_a_stalled_fpga():
    running = format_status(
        _status(fpga_active=True, fpga_running=True), DaemonMode.WIFI)
    stalled = format_status(
        _status(fpga_active=True, fpga_running=False), DaemonMode.WIFI)
    off = format_status(_status(fpga_active=False), DaemonMode.WATERFALL)

    assert "fpga:       on (running)" in running
    assert "fpga:       on (not running)" in stalled
    assert "fpga:       off" in off


def test_format_tx_status_idle():
    text = format_tx_status(_status(tx_atten_db=12.5), None)
    assert "tx:         idle" in text
    assert "attenuation: 12.5 dB" in text


def test_format_tx_status_busy():
    text = format_tx_status(
        _status(tx_busy=1, tx_proto=0, tx_atten_db=10.0), None)
    assert "tx:         busy (wifi)" in text


def test_format_caps_has_builds_features_and_limits():
    caps = Capabilities(
        fpga_build_id=7, features=15, max_sample_rate=61_440_000,
        max_fft_size=4096, min_freq_mhz=70, max_freq_mhz=6000,
        fw_build_id=42,
    )
    text = format_caps(caps)
    assert "firmware build: 42" in text
    assert "fpga build:     7" in text
    assert "waterfall, wifi decode, fabric wifi, iq" in text
    assert "frequency:      70 - 6000 MHz" in text


def test_agc_name_to_wire_code():
    from shesha.cli.common import AGC_CODES
    assert AGC_CODES == {"manual": 0, "fast": 1, "slow": 2}


def test_status_dict_has_tx_and_iq_fields():
    from shesha.client import _status_to_dict
    data = _status_to_dict(
        _status(tx_busy=1, tx_proto=0, tx_atten_db=12.5, iq_bits=8,
                psd_coverage=0.75, workers=2, clip_count=3,
                sample_count=100),
        DaemonMode.WIFI)
    assert data["tx_busy"] is True
    assert data["tx_proto_name"] == "wifi"
    assert data["tx_atten_db"] == 12.5
    assert data["iq_bits"] == 8
    assert data["psd_coverage"] == 0.75
    assert data["workers"] == 2
    assert data["clip_count"] == 3
    assert data["fpga_running"] is False


def test_status_dict_names_iq_tx():
    from shesha.client import _status_to_dict
    data = _status_to_dict(
        _status(tx_busy=1, tx_proto=2), DaemonMode.IQ)
    assert data["tx_busy"] is True
    assert data["tx_proto"] == 2
    assert data["tx_proto_name"] == "iq"


def test_caps_dict_has_all_feature_bits():
    from shesha.client import _caps_to_dict
    caps = Capabilities(
        fpga_build_id=7, features=15, max_sample_rate=61_440_000,
        max_fft_size=4096, min_freq_mhz=70, max_freq_mhz=6000,
        fw_build_id=42,
    )
    data = _caps_to_dict(caps)
    assert data["feature_names"] == [
        "waterfall", "wifi decode", "fabric wifi", "iq"]
    assert data["features_fabric_wifi"] is True
    assert data["features_iq"] is True
