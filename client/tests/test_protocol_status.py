"""RESP_STATUS must distinguish configured mode from what is actually running.

During an exclusive FM TX the daemon stops the RX producer but `g_mode`
still names it, so a client polling status was told RX was running when it
was not.  Any client-side policy would be reasoning from a false state.

`daemon_mode` remains the configured mode; `rx_active` reports whether that
mode's producer is actually running right now.
"""
import struct

from shesha.protocol import parse_status, _STATUS_STRUCT


def _status_bytes(*, freq_hz: int = 100_000_000,
                  rate_hz: int = 20_000_000, bw_hz: int = 20_000_000,
                  fft_size: int = 4096, gain_db: float = 30.0,
                  agc: int = 1, window: int = 1, fps: int = 50,
                  mode: int = 0, tx_atten_db: float = 20.0,
                  tx_busy: int = 0, tx_proto: int = 0,
                  tx_flags: int = 0, tx_repeat: int = 0,
                  tx_interval_us: int = 0,
                  fpga: bool = False, rx_active: bool = True,
                  iq_bits: int = 12, cover_permille: int = 1000,
                  workers: int = 1,
                  bw_min: int = 200_000, bw_max: int = 56_000_000,
                  bw_fixed: int = 0,
                  sr_min: int = 0, sr_max: int = 0, sr_fixed: int = 0,
                  sr_independent: int = 0, agc_fixed: int = 0,
                  clip_count: int = 0, sample_count: int = 0,
                  accum: int = 1, fpga_running: bool = False) -> bytes:
    """Build a RESP_STATUS payload from firmware/src/main.c offsets.

    The offsets and widths are written out here instead of reusing
    _STATUS_STRUCT.  Reusing the parser's struct would move the expected
    bytes whenever the parser moved, hiding a swap of two same-size fields.
    """
    b = bytearray(91)
    struct.pack_into('<Q', b, 0, freq_hz)
    struct.pack_into('<I', b, 8, rate_hz)
    struct.pack_into('<I', b, 12, bw_hz)
    struct.pack_into('<I', b, 16, fft_size)
    struct.pack_into('<f', b, 20, gain_db)
    b[24] = agc
    b[25] = window
    b[26] = fps
    b[27] = mode
    struct.pack_into('<f', b, 28, tx_atten_db)
    b[32] = tx_busy
    b[33] = tx_proto
    b[34] = tx_flags
    struct.pack_into('<H', b, 35, tx_repeat)
    struct.pack_into('<I', b, 37, tx_interval_us)
    b[41] = int(fpga)
    b[42] = int(rx_active)
    b[43] = iq_bits
    struct.pack_into('<H', b, 44, cover_permille)
    b[46] = workers
    struct.pack_into('<I', b, 47, bw_min)
    struct.pack_into('<I', b, 51, bw_max)
    struct.pack_into('<I', b, 55, bw_fixed)
    struct.pack_into('<I', b, 59, sr_min)
    struct.pack_into('<I', b, 63, sr_max)
    struct.pack_into('<I', b, 67, sr_fixed)
    b[71] = sr_independent
    b[72] = agc_fixed
    struct.pack_into('<Q', b, 73, clip_count)
    struct.pack_into('<Q', b, 81, sample_count)
    b[89] = accum
    b[90] = int(fpga_running)
    return bytes(b)


def test_rx_active_true_when_producer_running():
    st = parse_status(_status_bytes(mode=3, rx_active=True))
    assert st.daemon_mode == 3
    assert st.rx_active is True


def test_rx_active_false_during_exclusive_tx():
    """The case that made status lie: mode says FM, but nothing is running."""
    st = parse_status(_status_bytes(mode=3, rx_active=False, tx_busy=1))
    assert st.daemon_mode == 3, "configured mode is still reported"
    assert st.rx_active is False, "but the producer is not running"
    assert st.tx_busy == 1


def test_rx_active_is_a_bool_not_an_int():
    st = parse_status(_status_bytes(rx_active=False))
    assert isinstance(st.rx_active, bool)


def test_status_struct_size_matches_firmware_layout():
    """91 bytes: 47, the 26-byte radio policy block, two u64 counters, and
    the accumulator/running bytes.

    Firmware builds this field-by-field in main.c; if the two disagree the
    parse silently yields garbage rather than raising.
    """
    assert _STATUS_STRUCT.size == 91


def test_every_field_lands_at_its_documented_offset():
    """Distinct values catch a swap of two same-size fields."""
    st = parse_status(_status_bytes(
        freq_hz=123_456_789, rate_hz=11_111_111, bw_hz=22_222_222,
        fft_size=2048, gain_db=12.5, agc=2, window=3, fps=17, mode=4,
        tx_atten_db=7.5, tx_busy=1, tx_proto=2, tx_flags=3, tx_repeat=4,
        tx_interval_us=5, fpga=True, rx_active=False, iq_bits=8,
        cover_permille=750, workers=6,
        bw_min=7, bw_max=8, bw_fixed=9,
        sr_min=10, sr_max=11, sr_fixed=12,
        sr_independent=1, agc_fixed=1,
        clip_count=13, sample_count=14, accum=2, fpga_running=True))

    assert st.freq_hz == 123_456_789
    assert st.sample_rate_hz == 11_111_111
    assert st.bw_hz == 22_222_222
    assert st.fft_size == 2048
    assert st.gain_db == 12.5
    assert st.agc_mode == 2
    assert st.window_type == 3
    assert st.target_fps == 17
    assert st.daemon_mode == 4
    assert st.tx_atten_db == 7.5
    assert st.tx_busy == 1
    assert st.tx_proto == 2
    assert st.tx_flags == 3
    assert st.tx_repeat == 4
    assert st.tx_interval_us == 5
    assert st.fpga_active is True
    assert st.rx_active is False
    assert st.iq_bits == 8
    assert st.psd_coverage == 0.75
    assert st.workers == 6
    assert (st.radio.bw_min, st.radio.bw_max, st.radio.bw_fixed,
            st.radio.sr_min, st.radio.sr_max, st.radio.sr_fixed,
            st.radio.sr_independent, st.radio.agc_fixed) == \
        (7, 8, 9, 10, 11, 12, True, 1)
    assert st.clip_count == 13
    assert st.sample_count == 14
    assert st.accum == 2
    assert st.fpga_running is True


def test_accum_round_trips():
    assert parse_status(_status_bytes(accum=2)).accum == 2


def test_fpga_running_is_separate_from_fpga_active():
    """fpga_active is configuration; fpga_running is actual state.

    The FPGA variant can be configured but suspended (for example during an
    exclusive FM TX), and it can fail to start.  Both must be reportable.
    """
    st = parse_status(_status_bytes(fpga=True, fpga_running=False))
    assert st.fpga_active is True
    assert st.fpga_running is False

    st = parse_status(_status_bytes(fpga=True, fpga_running=True))
    assert st.fpga_active is True
    assert st.fpga_running is True

    st = parse_status(_status_bytes(fpga=False, fpga_running=False))
    assert st.fpga_active is False
    assert st.fpga_running is False


def test_fpga_running_is_a_bool_not_an_int():
    st = parse_status(_status_bytes(fpga_running=False))
    assert isinstance(st.fpga_running, bool)


def test_adc_clip_counters_round_trip():
    st = parse_status(_status_bytes(clip_count=1234, sample_count=50_000))
    assert st.clip_count == 1234
    assert st.sample_count == 50_000


def test_radio_policy_round_trips():
    payload = _status_bytes(
        bw_min=200_000, bw_max=8_000_000, bw_fixed=0,
        sr_min=2_100_000, sr_max=4_000_000, sr_fixed=0,
        sr_independent=1, agc_fixed=1)
    p = parse_status(payload).radio
    assert p.allows_bw(8_000_000)
    assert not p.allows_bw(9_000_000)
    assert p.allows_sr(2_100_000)
    assert not p.allows_sr(20_000_000)
    assert p.bw_is_user_input
    assert p.sr_is_user_input
    assert p.agc_fixed == 1
    assert p.agc_locked


def test_agc_unlocked_by_default():
    p = parse_status(_status_bytes()).radio
    assert p.agc_fixed == 0
    assert not p.agc_locked


def test_derived_rate_is_not_a_user_input():
    p = parse_status(_status_bytes()).radio
    assert p.bw_is_user_input
    assert not p.sr_is_user_input
    assert not p.allows_sr(20_000_000)


def test_fixed_policy_only_admits_its_exact_value():
    p = parse_status(_status_bytes(
        bw_fixed=200_000, sr_fixed=2_100_000)).radio
    assert p.allows_bw(200_000)
    assert not p.allows_bw(2_000_000)
    assert p.allows_sr(2_100_000)
    assert not p.allows_sr(20_000_000)
    assert not p.bw_is_user_input
    assert not p.sr_is_user_input


def test_workers_round_trips():
    """The daemon reports the live worker count, not a target."""
    assert parse_status(_status_bytes(workers=1)).workers == 1
    assert parse_status(_status_bytes(workers=2)).workers == 2


def test_coverage_is_scaled_to_a_fraction():
    """The wire carries tenths of a percent; callers want 0.0-1.0."""
    assert parse_status(_status_bytes(cover_permille=1000)).psd_coverage == 1.0
    assert parse_status(_status_bytes(cover_permille=276)).psd_coverage == 0.276
    assert parse_status(_status_bytes(cover_permille=0)).psd_coverage == 0.0


def test_parse_rejects_a_short_payload():
    with pytest.raises(struct.error):
        parse_status(_status_bytes()[:-1])


import pytest  # noqa: E402  (used by the test above)
