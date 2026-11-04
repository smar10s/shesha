"""Handler-level tests for the headless CLI.

These use a fake DaemonClient so they can assert ordering and restore
behavior without a radio.
"""
from __future__ import annotations

import argparse
import struct
import time

import pytest

from shesha.cli import (
    common, debug, fm, iq, radio, status as status_cli, viz, wifi,
)
from shesha.client import DaemonClient, PushResult
from shesha.frames import build_beacon, build_deauth, parse_mac
from shesha.protocol import (
    DaemonMode, FrameDecodeError, IqfFrame, MsgType, PsdFrame, RDS_WIRE_SIZE,
    RadioPolicy, Status, TX_PROTO_WIFI, TX_STOP_ALL, ThreadStat, ThreadStats,
    TxStats, WifiDiag, WifiFrame, WifiIeSummary, WifiMeta,
)


def status(**kw) -> Status:
    base = dict(
        freq_hz=100_100_000, sample_rate_hz=20_000_000, bw_hz=20_000_000,
        fft_size=4096, gain_db=30.0, agc_mode=0, window_type=1,
        target_fps=20, daemon_mode=DaemonMode.WATERFALL, tx_atten_db=10.0,
        tx_busy=0, tx_proto=0, tx_flags=0, tx_repeat=0, tx_interval_us=0,
        fpga_active=False, rx_active=True, iq_bits=12, psd_coverage=1.0,
        workers=1,
        radio=RadioPolicy(0, 0, 20_000_000, 0, 0, 20_000_000, False, 0),
        clip_count=0, sample_count=0, accum=1, fpga_running=False,
    )
    base.update(kw)
    return Status(**base)


def tx_stats(**kw) -> TxStats:
    base = dict(
        tx_active=False, fm_active=False, end_of_stream=False, dac_rate=0,
        fed=0, emitted=0, elapsed_us=0, depth=0, max_depth=0, high_water=0,
        underruns=0, ring_fill=0, ring_size=0, audio_rate=0,
        audio_consumed=0, starved_samples=0, push_rejects=0,
        iq_active=False, iq_ring_fill=0, iq_push_rejects=0, iq_starved=0,
    )
    base.update(kw)
    return TxStats(**base)


def thread_stats(wall_us, *, tid=1, jiffies=0, total=0, idle=0,
                 switches=0) -> ThreadStats:
    return ThreadStats(
        wall_us=wall_us, cpu_total_jiffies=total,
        cpu_idle_jiffies=idle, clk_tck=100, vm_rss_kb=1024, n_cpus=2,
        threads=[ThreadStat(
            name="worker", tid=tid, utime_jiffies=jiffies,
            stime_jiffies=0, voluntary_switches=switches,
            involuntary_switches=0)])


class FakeClient:
    def __init__(self, *, mode=DaemonMode.WATERFALL, **status_kw):
        self.mode = int(mode)
        self._status = status(daemon_mode=self.mode, **status_kw)
        self.calls: list[tuple] = []
        self.recv_frames: list[tuple[int, bytes]] = []
        self.tx_stats_values: list[TxStats] = []
        self.stream_result = FakeStreamResult()
        self.disconnected = False
        self.fpga_ok = True
        self.diag_value = WifiDiag(0, 0, 0, 0, 0.0, 0.0)
        self.thread_samples: list[ThreadStats] = []
        self.dropped = 0

    @property
    def connected(self):
        return not self.disconnected

    def status(self):
        self._status.daemon_mode = self.mode
        return self._status, self.mode

    def connect(self):
        pass

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.disconnected = True

    def set_mode(self, mode):
        self.calls.append(("set_mode", int(mode)))
        self.mode = int(mode)
        return True

    def set_bw(self, hz):
        self.calls.append(("set_bw", hz))
        self._status.bw_hz = hz
        return True

    def set_sample_rate(self, hz):
        self.calls.append(("set_sample_rate", hz))
        self._status.sample_rate_hz = hz
        return True

    def set_freq(self, hz):
        self.calls.append(("set_freq", hz))
        self._status.freq_hz = hz
        return True

    def set_gain(self, db):
        self.calls.append(("set_gain", db))
        self._status.gain_db = db
        return True

    def set_agc(self, code):
        self.calls.append(("set_agc", code))
        self._status.agc_mode = code
        return True

    def set_fpga(self, enable):
        self.calls.append(("set_fpga", enable))
        if not self.fpga_ok:
            return False
        self._status.fpga_active = enable
        return True

    def set_iq_bits(self, bits):
        self.calls.append(("set_iq_bits", bits))
        self._status.iq_bits = bits
        return True

    def set_window(self, window):
        self.calls.append(("set_window", int(window)))
        return True

    def set_accum(self, mode):
        self.calls.append(("set_accum", int(mode)))
        return True

    def set_workers(self, workers):
        self.calls.append(("set_workers", workers))
        return True

    def set_tx_atten(self, db):
        self.calls.append(("set_tx_atten", db))
        self._status.tx_atten_db = db
        return True

    def subscribe(self, mask):
        self.calls.append(("subscribe", mask))
        return True

    def tx_stop(self, target=TX_STOP_ALL):
        self.calls.append(("tx_stop", target))
        return True

    def recv_raw(self, timeout=0.0):
        frames, self.recv_frames = self.recv_frames, []
        return frames

    def stream(self, duration=5.0, verbose=False, psd_sink=None,
               wifi_sink=None, fm_sink=None, iq_sink=None):
        self.stream_result.duration = duration
        if psd_sink is not None:
            for frame in self.stream_result.psd_frames:
                psd_sink(frame)
        if wifi_sink is not None:
            for frame in self.stream_result.wifi_frames:
                wifi_sink(frame)
        return self.stream_result

    def iq_rx(self, duration=0.0):
        self.calls.append(("iq_rx", duration))
        yield IqfFrame(
            timestamp=1.0, freq_mhz=2_400.0, sample_rate_hz=2_100_000,
            bits=8, n_samples=4, samples=b'\x00' * 8)

    def wifi_tx(self, rate, psdu, repeat=1, interval_us=0, cyclic=False):
        self.calls.append(
            ("wifi_tx", rate, psdu, repeat, interval_us, cyclic))
        return True

    def diag(self):
        self.calls.append(("diag",))
        return self.diag_value

    def thread_stats(self):
        self.calls.append(("thread_stats",))
        return self.thread_samples.pop(0)

    def wait(self, duration):
        self.calls.append(("wait", duration))

    def iq_tx_stream_data(self, data):
        self.calls.append(("iq_tx_stream_data", len(data)))
        return PushResult.OK

    def iq_tx_stream_end(self):
        self.calls.append(("iq_tx_stream_end",))
        return True

    def tx_stats(self):
        if self.tx_stats_values:
            return self.tx_stats_values.pop(0)
        return tx_stats()


class _FakePsdAggregate:
    def __init__(self, owner):
        self._owner = owner

    @property
    def frames(self):
        return len(self._owner.psd_frames)

    @property
    def bin_count(self):
        return len(self._owner.psd_frames[0].bins) if self.frames else 0

    @property
    def mean_db(self):
        bins = [p for f in self._owner.psd_frames for p in f.bins]
        return sum(bins) / len(bins) if bins else None

    @property
    def peak_db(self):
        bins = [p for f in self._owner.psd_frames for p in f.bins]
        return max(bins) if bins else None

    @property
    def peak_bin(self):
        bins = self._owner.psd_frames[0].bins if self.frames else []
        flat = [p for f in self._owner.psd_frames for p in f.bins]
        return flat.index(max(flat)) % len(bins) if bins else 0

    @property
    def peak_center_mhz(self):
        return max(
            self._owner.psd_frames,
            key=lambda f: max(f.bins, default=float("-inf")),
        ).center_freq_mhz if self.frames else 0.0

    @property
    def last_center_mhz(self):
        return self._owner.psd_frames[-1].center_freq_mhz \
            if self.frames else 0.0


class FakeStreamResult:
    def __init__(self):
        self.duration = 1.0
        self.mode = int(DaemonMode.WIFI)
        self.wifi_frames = []
        self.wifi_count = 0
        self.wifi_fcs_ok = 0
        self.fm_count = 0
        self.iq_count = 0
        self.psd_frames = []
        self.psd = _FakePsdAggregate(self)
        self.errors = []

    @property
    def psd_count(self):
        return len(self.psd_frames)

    @property
    def psd_fps(self):
        return self.psd_count / self.duration if self.duration > 0 else 0.0

    @property
    def wifi_fps(self):
        return 0.0

    def to_dict(self, verbose=False):
        return {"duration": self.duration, "mode": self.mode}


class FakePlayer:
    available = False
    label = "fake"
    dropped = 0
    player_gone = False

    def __init__(self, rate):
        self.rate = rate

    def push(self, audio):
        pass

    def close(self):
        pass


def _fm_payload():
    hdr = struct.pack('<dII', 1.5, 43750, 1)
    return hdr + bytes(RDS_WIRE_SIZE) + struct.pack('<f', 0.0)


def _patch_connect(monkeypatch, module, client):
    monkeypatch.setattr(module, "_connect", lambda args: client)


def frame_args(**kw) -> argparse.Namespace:
    base = dict(
        type="beacon", channel=6, freq=None, rate=6, count=1, interval=0,
        cyclic=False, ssid="SHESHA_TEST", bssid=None, da=None, sa=None,
        seq=0, capability=0x0401, beacon_interval=100, ie=[],
        reason=7, psdu_file=None, psdu_hex=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _tx_call(c):
    return next(call for call in c.calls if call[0] == "wifi_tx")


def test_radio_gain_switches_agc_to_manual(monkeypatch, capsys):
    c = FakeClient()
    _patch_connect(monkeypatch, radio, c)

    radio.do_gain(argparse.Namespace(db=40.0, json=False))

    assert c.calls[:2] == [("set_agc", 0), ("set_gain", 40.0)]
    assert "gain: 40.0 dB (manual)" in capsys.readouterr().out


def test_radio_agc_maps_name_to_code(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, radio, c)

    radio.do_agc(argparse.Namespace(mode="fast", json=False))

    assert ("set_agc", 1) in c.calls


def test_radio_sample_rate_is_msps(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, radio, c)

    radio.do_sample_rate(argparse.Namespace(msps=20.0, json=False))

    assert ("set_sample_rate", 20_000_000) in c.calls


def test_scan_sets_mode_before_agc_and_gain(monkeypatch, capsys):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_scan(argparse.Namespace(
        freq=None, channel=None, gain=40.0, agc=None, duration=0.1,
        json=False))

    calls = c.calls
    assert calls.index(("set_mode", DaemonMode.WIFI)) < \
        calls.index(("set_agc", 0)) < calls.index(("set_gain", 40.0))
    assert ("set_mode", DaemonMode.WATERFALL) in calls
    assert ("set_bw", 20_000_000) in calls
    assert ("set_sample_rate", 20_000_000) in calls


def test_scan_defaults_to_channel_6(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_scan(argparse.Namespace(
        freq=None, channel=None, gain=None, agc=None, duration=0.1,
        json=False))

    assert ("set_freq", 2_437_000_000) in c.calls


def test_scan_json_includes_setup(monkeypatch, capsys):
    import json
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_scan(argparse.Namespace(
        freq=2437.0, channel=None, gain=None, agc=None, duration=0.1,
        json=True))

    out = json.loads(capsys.readouterr().out)
    assert out["freq_hz"] == 2_437_000_000
    assert out["duration_s"] == 0.1
    assert out["agc"] == "fast"


def _wifi_beacon_frame(bssid: str, ssid: str) -> WifiFrame:
    return WifiFrame(
        timestamp=1.0, rate_code=0, rate_mbps=6, fcs_ok=True, length=100,
        frame_class=3, frame_class_name="beacon", rx_gain=50.0,
        rx_freq_hz=2_437_000_000, psdu=b"",
        meta=WifiMeta(bssid=bssid, sa=bssid, da="ff:ff:ff:ff:ff:ff",
                      ssid=ssid, is_eapol=False),
        ie=WifiIeSummary(
            channel=6, security="open", cipher="none", phy_flags=0, phy="",
            max_rate=0.0, n_ss=0, chan_width=0, center_chan=0,
            secondary_offset=0, country="", pmf="none"))


def test_scan_json_includes_grouping_and_manufacturer(monkeypatch, capsys):
    import json
    c = FakeClient()
    c.stream_result.wifi_frames = [
        _wifi_beacon_frame("10:78:5b:fd:b6:26", "Home"),
        _wifi_beacon_frame("12:78:5b:fd:b6:26", "Home"),
    ]
    c.stream_result.wifi_count = 2
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_scan(argparse.Namespace(
        freq=2437.0, channel=None, gain=None, agc=None, duration=0.1,
        json=True))

    aps = json.loads(capsys.readouterr().out)["aps"]
    assert len(aps) == 2
    assert all(ap["group_id"] >= 0 for ap in aps)
    assert all(ap["manufacturer"] for ap in aps)


def test_fm_rx_sets_mode_before_gain_and_restores(monkeypatch, capsys):
    c = FakeClient()
    c.recv_frames.append((MsgType.DATA_FM, _fm_payload()))
    _patch_connect(monkeypatch, fm, c)
    monkeypatch.setattr(fm, "AudioPlayer", FakePlayer)

    with pytest.raises(SystemExit):
        fm.do_fm_rx(argparse.Namespace(
            freq=96.7, gain=40.0, duration=1.0, wav=None, volume=1.0))

    calls = c.calls
    assert calls.index(("set_mode", DaemonMode.FM)) < \
        calls.index(("set_agc", 0)) < calls.index(("set_gain", 40.0))
    assert ("set_mode", DaemonMode.WATERFALL) in calls


def test_iq_tx_drains_before_disconnect(monkeypatch, tmp_path):
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 2)
    c = FakeClient()
    c.tx_stats_values = [
        tx_stats(iq_active=True),
        tx_stats(iq_active=True),
        tx_stats(iq_active=False),
    ]
    _patch_connect(monkeypatch, iq, c)

    iq.do_iq_tx(argparse.Namespace(
        file=str(path), bits=8, sr=2.1, freq=2400.0, tx_atten=None,
        channel=None, json=False))

    assert not [call for call in c.calls if call[0] == "tx_stop"]
    assert c.calls[-1] == ("disconnect",)
    assert ("set_sample_rate", 2_100_000) in c.calls
    assert ("set_mode", DaemonMode.WATERFALL) in c.calls


def test_iq_tx_reads_sidecar_when_flags_are_omitted(monkeypatch, tmp_path,
                                                    capsys):
    import json
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 6)
    (tmp_path / "iq.bin.meta.json").write_text(json.dumps({
        "freq_hz": 915_000_000, "sample_rate_hz": 2_100_000, "iq_bits": 8,
    }))
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    iq.do_iq_tx(argparse.Namespace(
        file=str(path), bits=None, sr=None, freq=None, channel=None,
        tx_atten=None, json=True))

    assert ("set_iq_bits", 8) in c.calls
    assert ("set_sample_rate", 2_100_000) in c.calls
    assert ("set_freq", 915_000_000) in c.calls
    assert json.loads(capsys.readouterr().out)["bits"] == 8


def test_iq_tx_flags_override_the_sidecar(monkeypatch, tmp_path):
    import json
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 6)
    (tmp_path / "iq.bin.meta.json").write_text(json.dumps({
        "freq_hz": 915_000_000, "sample_rate_hz": 2_100_000, "iq_bits": 8,
    }))
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    iq.do_iq_tx(argparse.Namespace(
        file=str(path), bits=12, sr=4.0, freq=100.0, channel=None,
        tx_atten=None, json=False))

    assert ("set_iq_bits", 12) in c.calls
    assert ("set_sample_rate", 4_000_000) in c.calls
    assert ("set_freq", 100_000_000) in c.calls


def test_iq_tx_defaults_to_12_bits_without_a_sidecar(monkeypatch, tmp_path):
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 6)
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    iq.do_iq_tx(argparse.Namespace(
        file=str(path), bits=None, sr=None, freq=None, channel=None,
        tx_atten=None, json=False))

    assert ("set_iq_bits", 12) in c.calls
    stream_idx = c.calls.index(("iq_tx_stream_data", 6))
    assert not [call for call in c.calls[:stream_idx]
                if call[0] == "set_sample_rate"]
    assert not [call for call in c.calls if call[0] == "set_freq"]


def test_iq_tx_ignores_a_bad_sidecar_when_all_flags_are_given(
        monkeypatch, tmp_path):
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 6)
    (tmp_path / "iq.bin.meta.json").write_text("{not json")
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    iq.do_iq_tx(argparse.Namespace(
        file=str(path), bits=12, sr=4.0, freq=100.0, channel=None,
        tx_atten=None, json=False))

    assert ("set_iq_bits", 12) in c.calls
    assert ("set_sample_rate", 4_000_000) in c.calls
    assert ("set_freq", 100_000_000) in c.calls


def test_iq_tx_rejects_a_bad_sidecar(monkeypatch, tmp_path, capsys):
    path = tmp_path / "iq.bin"
    path.write_bytes(b"\x00" * 6)
    (tmp_path / "iq.bin.meta.json").write_text("{not json")
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    with pytest.raises(SystemExit):
        iq.do_iq_tx(argparse.Namespace(
            file=str(path), bits=None, sr=None, freq=None, channel=None,
            tx_atten=None, json=False))

    assert "sidecar" in capsys.readouterr().err


def test_iq_tx_missing_file_is_a_clean_error(monkeypatch, capsys):
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)

    with pytest.raises(SystemExit):
        iq.do_iq_tx(argparse.Namespace(
            file="/nonexistent/iq.bin", bits=8, sr=None, freq=None,
            tx_atten=None, channel=None, json=False))

    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Traceback" not in err


def test_restore_radio_restores_settings(monkeypatch):
    c = FakeClient(mode=DaemonMode.FM)
    state = common.RadioState(
        mode=DaemonMode.WATERFALL, bw_hz=20_000_000,
        sample_rate_hz=20_000_000, agc_mode=0, gain_db=30.0,
        fpga_active=False)

    common.restore_radio(c, state)

    assert c.calls == [
        ("set_mode", DaemonMode.WATERFALL),
        ("set_bw", 20_000_000),
        ("set_sample_rate", 20_000_000),
        ("set_agc", 0),
        ("set_gain", 30.0),
    ]


def test_restore_radio_is_a_noop_in_the_same_mode(monkeypatch):
    c = FakeClient(mode=DaemonMode.WATERFALL)
    state = common.RadioState(
        mode=DaemonMode.WATERFALL, bw_hz=20_000_000,
        sample_rate_hz=20_000_000, agc_mode=0, gain_db=30.0,
        fpga_active=False)

    common.restore_radio(c, state)

    assert c.calls == []


def test_restore_radio_reenables_fpga():
    c = FakeClient(mode=DaemonMode.FM)
    state = common.RadioState(
        mode=DaemonMode.WIFI, bw_hz=28_000_000,
        sample_rate_hz=20_000_000, agc_mode=1, gain_db=30.0,
        fpga_active=True)

    common.restore_radio(c, state)

    assert c.calls == [
        ("set_mode", DaemonMode.WIFI),
        ("set_fpga", True),
        ("set_bw", 28_000_000),
        ("set_sample_rate", 20_000_000),
        ("set_agc", 1),
    ]


def test_restore_radio_tolerates_a_dead_socket():
    """Cleanup runs in a finally; a dead socket must not raise there."""
    state = common.RadioState(
        mode=DaemonMode.WATERFALL, bw_hz=20_000_000,
        sample_rate_hz=20_000_000, agc_mode=0, gain_db=30.0,
        fpga_active=False)

    common.restore_radio(DaemonClient(), state)


def _drop_after_first_frame(c):
    """recv_raw that yields one FM frame, then marks the link dead."""
    def recv_raw(timeout=0.0):
        frames, c.recv_frames = c.recv_frames, []
        c.disconnected = True
        return frames
    return recv_raw


def test_fm_rx_fails_fast_when_link_is_down(monkeypatch, capsys):
    c = FakeClient()
    c.disconnected = True
    _patch_connect(monkeypatch, fm, c)

    started = time.monotonic()
    with pytest.raises(SystemExit):
        fm.do_fm_rx(argparse.Namespace(
            freq=96.7, gain=None, duration=0.0, wav=None, volume=1.0,
            json=True))

    assert time.monotonic() - started < 0.5


def test_fm_rx_stops_when_link_drops(monkeypatch, capsys):
    c = FakeClient()
    c.recv_frames = [(MsgType.DATA_FM, _fm_payload())]
    c.recv_raw = _drop_after_first_frame(c)
    _patch_connect(monkeypatch, fm, c)

    class PlayingPlayer(FakePlayer):
        available = True

    monkeypatch.setattr(fm, "AudioPlayer", PlayingPlayer)

    started = time.monotonic()
    fm.do_fm_rx(argparse.Namespace(
        freq=96.7, gain=None, duration=1.0, wav=None, volume=1.0,
        json=True))

    assert time.monotonic() - started < 0.5


def test_rds_stops_when_link_drops(monkeypatch, capsys):
    c = FakeClient()
    c.recv_frames = [(MsgType.DATA_FM, _fm_payload())]
    c.recv_raw = _drop_after_first_frame(c)
    _patch_connect(monkeypatch, fm, c)

    started = time.monotonic()
    fm.do_rds(argparse.Namespace(
        freq=96.7, channel=None, gain=None, duration=1.0, json=True))

    assert time.monotonic() - started < 0.5


def test_resolve_freq_channel():
    assert common.resolve_freq(argparse.Namespace(channel=6, freq=None)) == 2437.0
    assert common.resolve_freq(argparse.Namespace(channel=None, freq=96.7)) == 96.7
    with pytest.raises(SystemExit):
        common.resolve_freq(argparse.Namespace(channel=999, freq=None))


def test_fm_rx_skips_bad_frames_and_keeps_streaming(monkeypatch, capsys):
    import json

    c = FakeClient()
    bad = _fm_payload()[:20]
    frames = [
        (MsgType.DATA_FM, bad),
        (MsgType.DATA_FM, _fm_payload()),
        (MsgType.DATA_FM, _fm_payload()),
        (MsgType.DATA_FM, bad),
        (MsgType.DATA_FM, _fm_payload()),
    ]

    def recv_raw(timeout=0.0):
        return [frames.pop(0)] if frames else []

    c.recv_raw = recv_raw
    _patch_connect(monkeypatch, fm, c)

    class PlayingPlayer(FakePlayer):
        available = True

    monkeypatch.setattr(fm, "AudioPlayer", PlayingPlayer)

    fm.do_fm_rx(argparse.Namespace(
        freq=96.7, gain=None, duration=0.05, wav=None, volume=1.0,
        json=True))

    summary = json.loads(capsys.readouterr().out)
    assert summary["frames"] == 1
    assert summary["decode_errors"] == 2


def test_rds_skips_a_bad_frame(monkeypatch, capsys):
    import json

    c = FakeClient()
    bad = _fm_payload()[:20]
    frames = [(MsgType.DATA_FM, bad), (MsgType.DATA_FM, _fm_payload())]

    def recv_raw(timeout=0.0):
        return [frames.pop(0)] if frames else []

    c.recv_raw = recv_raw
    _patch_connect(monkeypatch, fm, c)

    fm.do_rds(argparse.Namespace(
        freq=96.7, channel=None, gain=None, duration=0.1, json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["freq_mhz"] == 96.7


def test_iq_rx_reports_a_bad_frame(monkeypatch, tmp_path, capsys):
    c = FakeClient()

    def bad_iq_rx(duration=0.0):
        raise FrameDecodeError("iq frame truncated")
        yield

    c.iq_rx = bad_iq_rx
    _patch_connect(monkeypatch, iq, c)

    with pytest.raises(SystemExit):
        iq.do_iq_rx(argparse.Namespace(
            out=str(tmp_path / "cap.bin"), duration=0.1, bits=8, sr=2.1,
            bw=8.0, gain=40.0, freq=None, channel=None, meta=False,
            json=True))

    assert "bad IQ frame" in capsys.readouterr().err


def test_iq_rx_reports_settings_and_restores(monkeypatch, tmp_path, capsys):
    import json
    c = FakeClient()
    _patch_connect(monkeypatch, iq, c)
    out = tmp_path / "cap.bin"

    iq.do_iq_rx(argparse.Namespace(
        out=str(out), duration=0.1, bits=8, sr=2.1, bw=8.0, gain=40.0,
        freq=None, channel=None, meta=False, json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["freq_hz"] == 100_100_000
    assert data["bw_hz"] == 8_000_000
    assert data["gain_db"] == 40.0
    assert data["agc"] == "manual"
    assert ("set_mode", DaemonMode.WATERFALL) in c.calls


def test_fm_rx_json_summary_streams_wav_chunks(monkeypatch, tmp_path, capsys):
    import json

    c = FakeClient()
    remaining = [(MsgType.DATA_FM, _fm_payload()) for _ in range(4)]

    def recv_raw(timeout=0.0):
        return [remaining.pop(0)] if remaining else []

    c.recv_raw = recv_raw
    _patch_connect(monkeypatch, fm, c)
    monkeypatch.setattr(fm, "AudioPlayer", FakePlayer)

    events = []

    class FakeWave:
        def __init__(self, path):
            self.path = path

        def setnchannels(self, n):
            pass

        def setsampwidth(self, n):
            pass

        def setframerate(self, n):
            self.rate = n

        def writeframes(self, data):
            events.append(("write", len(data)))

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(fm.wave, "open",
                        lambda path, mode: FakeWave(path))
    wav_path = str(tmp_path / "out.wav")

    fm.do_fm_rx(argparse.Namespace(
        freq=96.7, gain=None, duration=0.05, wav=wav_path, volume=1.0,
        json=True))

    summary = json.loads(capsys.readouterr().out)
    assert summary["audio_rate_hz"] == 43750
    assert summary["frames"] == 2
    assert summary["samples"] == 2
    assert summary["wav"] == wav_path
    assert events[0][0] == "write"
    assert events[-1] == ("close",)
    assert sum(event[1] for event in events if event[0] == "write") == 4


def test_fm_rx_json_reports_a_clean_live_summary(monkeypatch, capsys):
    import json

    c = FakeClient()
    remaining = [(MsgType.DATA_FM, _fm_payload()) for _ in range(4)]

    def recv_raw(timeout=0.0):
        return [remaining.pop(0)] if remaining else []

    c.recv_raw = recv_raw
    _patch_connect(monkeypatch, fm, c)

    class PlayingPlayer(FakePlayer):
        available = True

    monkeypatch.setattr(fm, "AudioPlayer", PlayingPlayer)

    fm.do_fm_rx(argparse.Namespace(
        freq=96.7, gain=None, duration=0.05, wav=None, volume=1.0,
        json=True))

    summary = json.loads(capsys.readouterr().out)
    assert summary["freq_mhz"] == 96.7
    assert summary["frames"] == 2
    assert summary["wav"] is None
    assert summary["realtime_pct"] >= 0.0


def test_rds_sets_mode_before_gain_and_restores(monkeypatch, capsys):
    import json
    c = FakeClient()
    c.recv_frames.append((MsgType.DATA_FM, _fm_payload()))
    _patch_connect(monkeypatch, fm, c)

    fm.do_rds(argparse.Namespace(
        freq=96.7, channel=None, gain=40.0, duration=0.2, json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["freq_mhz"] == 96.7
    assert data["pty_name"] is None
    calls = c.calls
    assert calls.index(("set_mode", DaemonMode.FM)) < \
        calls.index(("set_agc", 0)) < calls.index(("set_gain", 40.0))
    assert ("set_mode", DaemonMode.WATERFALL) in calls


def test_viz_window_maps_name_to_enum(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, viz, c)

    viz.do_window(argparse.Namespace(window="hann", json=False))

    assert ("set_window", 1) in c.calls


def test_viz_accum_maps_name_to_enum(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, viz, c)

    viz.do_accum(argparse.Namespace(accum="avg", json=False))

    assert ("set_accum", 2) in c.calls


def test_viz_workers_requests_a_count(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, viz, c)

    viz.do_workers(argparse.Namespace(workers=2, json=False))

    assert ("set_workers", 2) in c.calls


def test_viz_capture_writes_jsonl_and_summary(monkeypatch, tmp_path, capsys):
    import json

    c = FakeClient()
    c.stream_result.psd_frames = [
        PsdFrame(1.0, 2437.0, -100.0, -10.0, [-80.0, -70.0, -60.0, -50.0]),
        PsdFrame(1.05, 2437.0, -100.0, -10.0, [-81.0, -71.0, -61.0, -51.0]),
    ]
    _patch_connect(monkeypatch, viz, c)
    out = tmp_path / "psd.jsonl"

    viz.do_capture(argparse.Namespace(duration=0.2, out=str(out), json=True))

    summary = json.loads(capsys.readouterr().out)
    assert summary["frames"] == 2
    assert summary["fps"] == 10.0
    assert summary["fft_size"] == 4
    assert summary["freq_mhz"] == 2437.0
    assert summary["coverage"] == 1.0
    assert summary["workers"] == 1
    assert summary["mean_db"] == -65.5
    assert summary["peak_db"] == -50.0
    assert summary["peak_freq_mhz"] == 2442.0

    lines = out.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["center_mhz"] == 2437.0
    assert first["bins"] == [-80.0, -70.0, -60.0, -50.0]


def test_viz_capture_fails_when_no_frames_arrive(monkeypatch, capsys):
    c = FakeClient()
    _patch_connect(monkeypatch, viz, c)

    with pytest.raises(SystemExit):
        viz.do_capture(argparse.Namespace(duration=0.2, out=None, json=False))

    assert "no PSD frames" in capsys.readouterr().err


def test_tx_status_prints_detail(monkeypatch, capsys):
    c = FakeClient(tx_busy=1, tx_proto=0, tx_atten_db=12.5)
    c.tx_stats_values = [tx_stats(tx_active=True, emitted=100, elapsed_us=1000,
                                  dac_rate=2_000_000)]
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_tx_status(argparse.Namespace(json=False))

    out = capsys.readouterr().out
    assert "tx:         busy (wifi)" in out
    assert "attenuation: 12.5 dB" in out
    assert "dac rate:    2.000 MSPS" in out


def test_fpga_failure_restores_previous_settings(monkeypatch):
    c = FakeClient(mode=DaemonMode.WATERFALL)
    c.fpga_ok = False
    _patch_connect(monkeypatch, wifi, c)

    with pytest.raises(SystemExit):
        wifi.do_fpga(argparse.Namespace(state="on", json=False))

    calls = c.calls
    assert ("set_mode", DaemonMode.WATERFALL) in calls
    assert ("set_bw", 20_000_000) in calls
    assert ("set_sample_rate", 20_000_000) in calls


def test_beacon_channel_and_freq_map_to_the_same_frame(monkeypatch, capsys):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_beacon(argparse.Namespace(
        ssid="TEST", channel=6, freq=None, rate=6, count=1, interval=0,
        cyclic=False, json=True))
    by_channel = capsys.readouterr().out

    c2 = FakeClient()
    _patch_connect(monkeypatch, wifi, c2)
    wifi.do_beacon(argparse.Namespace(
        ssid="TEST", channel=None, freq=2437.0, rate=6, count=1, interval=0,
        cyclic=False, json=True))
    by_freq = capsys.readouterr().out

    assert ("set_freq", 2_437_000_000) in c.calls
    assert ("set_freq", 2_437_000_000) in c2.calls
    assert '"channel": 6' in by_channel
    assert '"channel": 6' in by_freq


def test_frame_deauth_uses_shared_builder(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)
    bssid = "aa:bb:cc:dd:ee:ff"
    da = "11:22:33:44:55:66"

    wifi.do_frame(frame_args(
        type="deauth", bssid=bssid, da=da, reason=3,
        count=5, interval=1000, cyclic=True))

    tx = _tx_call(c)
    assert tx[1] == 6
    assert tx[2] == build_deauth(parse_mac(da), parse_mac(bssid), reason=3)
    assert tx[3:] == (5, 1000, True)
    assert ("set_freq", 2_437_000_000) in c.calls


def test_frame_beacon_uses_shared_builder(monkeypatch, capsys):
    import json

    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_frame(frame_args(
        type="beacon", freq=2437.0, ssid="TEST",
        bssid="aa:bb:cc:dd:ee:ff", seq=7, capability=0x0411,
        beacon_interval=200, ie=["221:0102"], json=True))

    psdu = _tx_call(c)[2]
    timestamp = int.from_bytes(psdu[24:32], "little")
    assert psdu == build_beacon(
        ssid="TEST", bssid=parse_mac("aa:bb:cc:dd:ee:ff"),
        channel=6, seq=7, capability=0x0411, beacon_interval=200,
        ies=[bytes.fromhex("dd020102")], timestamp=timestamp)
    assert ("set_freq", 2_437_000_000) in c.calls

    out = json.loads(capsys.readouterr().out)
    assert out["type"] == "beacon"
    assert out["channel"] == 6
    assert out["freq_mhz"] == 2437.0
    assert out["rate_mbps"] == 6
    assert out["repeat"] == 1


def test_frame_raw_accepts_hex_with_separators(monkeypatch):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_frame(frame_args(type="raw", psdu_hex="80:00 aa-bb"))

    assert _tx_call(c)[2] == bytes.fromhex("8000aabb")


def test_frame_raw_reads_a_psdu_file(monkeypatch, tmp_path):
    path = tmp_path / "frame.bin"
    path.write_bytes(bytes.fromhex("8000cafe"))
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_frame(frame_args(type="raw", psdu_file=str(path)))

    assert _tx_call(c)[2] == bytes.fromhex("8000cafe")


@pytest.mark.parametrize(
    "args, message",
    [
        (frame_args(type="raw"), "requires --psdu-file or --psdu-hex"),
        (frame_args(type="deauth", bssid=None, da="11:22:33:44:55:66"),
         "requires --bssid"),
        (frame_args(type="deauth", bssid="aa:bb:cc:dd:ee:ff"),
         "requires --da"),
        (frame_args(type="raw", psdu_hex="zz"), "invalid --psdu-hex"),
        (frame_args(type="beacon", ie=["221"]), "invalid --ie"),
        (frame_args(type="beacon", channel=999), "unknown WiFi channel"),
        (frame_args(type="beacon", channel=None, freq=100.1),
         "not a WiFi channel"),
        (frame_args(type="deauth", bssid="nope", da="11:22:33:44:55:66"),
         "invalid --bssid"),
    ],
)
def test_frame_rejects_invalid_arguments(monkeypatch, capsys, args, message):
    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    with pytest.raises(SystemExit):
        wifi.do_frame(args)

    assert message in capsys.readouterr().err
    assert c.calls == []


def test_wifi_tx_stop_targets_only_wifi(monkeypatch, capsys):
    import json

    c = FakeClient()
    _patch_connect(monkeypatch, wifi, c)

    wifi.do_beacon_stop(argparse.Namespace(json=True))

    assert ("tx_stop", TX_PROTO_WIFI) in c.calls
    data = json.loads(capsys.readouterr().out)
    assert data == {"stopped": True, "protocol": "wifi"}


def test_fm_tx_rejects_zero_rate(monkeypatch, tmp_path, capsys):
    path = tmp_path / "tone.f32"
    path.write_bytes(b"\x00\x00\x00\x00" * 4)
    c = FakeClient()
    _patch_connect(monkeypatch, fm, c)

    with pytest.raises(SystemExit):
        fm.do_fm_tx(argparse.Namespace(
            freq=100.1, channel=None, file=str(path), rate=0,
            tx_atten=None, json=False))

    assert "rate must be positive" in capsys.readouterr().err


def test_fm_tx_keeps_the_push_error_after_ending_the_stream(
        monkeypatch, tmp_path, capsys):
    path = tmp_path / "tone.f32"
    path.write_bytes(struct.pack('<10f', *([0.1] * 10)))
    c = FakeClient()
    c.fm_tx_stream_start = lambda *a, **k: True

    def push(_audio):
        c.last_error = "IQ tx is active"
        return PushResult.ERROR

    def end():
        c.calls.append(("fm_tx_stream_end",))
        c.last_error = None
        return True

    c.fm_tx_stream_push = push
    c.fm_tx_stream_end = end
    _patch_connect(monkeypatch, fm, c)

    with pytest.raises(SystemExit):
        fm.do_fm_tx(argparse.Namespace(
            freq=100.1, channel=None, file=str(path), rate=3,
            tx_atten=None, json=False))

    assert "IQ tx is active" in capsys.readouterr().err
    assert ("fm_tx_stream_end",) in c.calls


def test_debug_cpu_reports_thread(monkeypatch, capsys):
    c = FakeClient()
    c.thread_samples = [
        thread_stats(1_000_000, jiffies=0, total=0, idle=0),
        thread_stats(2_000_000, jiffies=50, total=200, idle=100,
                     switches=10),
    ]
    _patch_connect(monkeypatch, debug, c)

    debug.do_cpu(argparse.Namespace(interval=1.0, count=1, json=False))

    out = capsys.readouterr().out
    assert "worker" in out
    assert ("wait", 1.0) in c.calls


def test_debug_diag_summary(monkeypatch, capsys):
    c = FakeClient()
    c.diag_value = WifiDiag(10, 8, 6, 2, 0.1, 0.4)
    _patch_connect(monkeypatch, debug, c)

    debug.do_diag(argparse.Namespace(
        freq=None, channel=None, gain=None, agc=None, duration=0.1,
        interval=0.5, continuous=False, quiet=False, json=False))

    out = capsys.readouterr().out
    assert "captures:  10" in out
    assert "decode_ok: 8 (80.0%)" in out


def test_debug_stream_summary(monkeypatch, capsys):
    c = FakeClient()
    c.stream_result.wifi_count = 3
    _patch_connect(monkeypatch, debug, c)

    debug.do_stream(argparse.Namespace(
        duration=1.0, mode=None, verbose=False, json=False))

    out = capsys.readouterr().out
    assert "wifi:    3 frames" in out


def test_status_failure_includes_daemon_reason(monkeypatch, capsys):
    c = FakeClient()
    c.last_error = "daemon busy"

    def down():
        return None

    c.status = down
    _patch_connect(monkeypatch, status_cli, c)

    with pytest.raises(SystemExit):
        status_cli.do_status(argparse.Namespace(json=False))

    assert "daemon busy" in capsys.readouterr().err


def test_check_includes_daemon_reason(capsys):
    class Fake:
        last_error = "gain locked by wifi_fpga"

    with pytest.raises(SystemExit):
        common.check(Fake(), False, "could not set gain")

    assert "could not set gain: gain locked by wifi_fpga" in \
        capsys.readouterr().err


def test_send_config_keeps_the_daemon_error(monkeypatch):
    c = DaemonClient()
    monkeypatch.setattr(
        c, "_request_response",
        lambda *a, **k: (MsgType.RESP_ERROR,
                         bytes([MsgType.CMD_SET_FREQ])
                         + b"freq outside range\x00"))

    assert c._send_config(b"", "freq") is False
    assert c.last_error == "freq outside range"
