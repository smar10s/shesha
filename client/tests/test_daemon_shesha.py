"""Tests for SheshaDaemon (wire-level fake server + mock subprocess).

The daemon is the source of truth: client state comes only from
RESP_STATUS, config is a write-only seed for a freshly started daemon,
and reconnecting to a running daemon sends no configuration at all.
"""
import json
import os
import socket
import struct
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from shesha.protocol import MsgType, build_frame, _STATUS_STRUCT, RadioPolicy
from shesha.tui.config import Config
from shesha.tui.daemon import SheshaDaemon

FRAME_HEADER = struct.Struct('<BII')


def _make_config(values=None):
    data = {'freq': '5180000000', 'gain': '30', 'bw': '20000000'}
    if values:
        data.update(values)
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    json.dump(data, f)
    f.close()
    return Config(f.name), f.name


def _policy_for_mode(mode: int, fpga: bool) -> RadioPolicy:
    """Mirror producer.c's per-mode radio descriptors."""
    if mode == 2:      # IQ
        return RadioPolicy(200_000, 8_000_000, 0,
                           2_100_000, 4_000_000, 0, True, 0)
    if mode == 3:      # FM
        return RadioPolicy(0, 0, 200_000, 0, 0, 2_100_000, False, 0)
    if mode == 1:      # WiFi: FPGA pins AGC, software does not
        return RadioPolicy(0, 0, 28_000_000 if fpga else 20_000_000,
                           0, 0, 20_000_000, False, 1 if fpga else 0)
    return RadioPolicy(200_000, 56_000_000, 0, 0, 0, 0, False, 0)


class FakeDaemon(threading.Thread):
    """Minimal protocol server emulating the firmware.

    Records every command received.  Auto-answers every SET_* with
    RESP_OK plus an unsolicited RESP_STATUS push (new firmware behavior);
    CMD_STATUS is answered with a RESP_STATUS of the current state.
    `bw_locked` makes CMD_SET_BW answer RESP_ERROR instead.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.host = '127.0.0.1'
        self.freq = 5_180_000_000
        self.sr = 20_000_000
        self.bw = 20_000_000
        self.fft_size = 4096
        self.gain = 30.0
        self.agc = 1
        self.mode = 0
        self.fpga = False
        self.bw_locked = False
        self.clip_count = 0
        self.sample_count = 0
        self.policy = _policy_for_mode(0, False)
        self.commands: list[tuple[int, bytes]] = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, 0))
        self.port = self._srv.getsockname()[1]
        self._srv.listen(1)
        self._conn = None

    def status_payload(self) -> bytes:
        p = self.policy
        return _STATUS_STRUCT.pack(
            self.freq, self.sr, self.bw, self.fft_size, self.gain,
            self.agc, 1, 50, self.mode, 20.0, 0, 0, 0, 0, 0,
            int(self.fpga), 1, 12, 1000, 1,
            p.bw_min, p.bw_max, p.bw_fixed,
            p.sr_min, p.sr_max, p.sr_fixed, int(p.sr_independent),
            p.agc_fixed, self.clip_count, self.sample_count,
            1, 0)  # accum=max, fpga_running

    def run(self):
        try:
            self._conn, _ = self._srv.accept()
        except OSError:
            return
        buf = b''
        while True:
            try:
                data = self._conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while len(buf) >= 9:
                mtype, plen, _seq = FRAME_HEADER.unpack_from(buf, 0)
                if len(buf) < 9 + plen:
                    break
                payload = buf[9:9 + plen]
                buf = buf[9 + plen:]
                self.handle(mtype, payload)

    def handle(self, mtype, payload):
        self.commands.append((mtype, payload))
        if mtype == MsgType.CMD_STATUS:
            self.send(MsgType.RESP_STATUS, self.status_payload())
            return
        if mtype == MsgType.CMD_SUBSCRIBE:
            self.send(MsgType.RESP_OK, bytes([mtype]))
            return
        if mtype == MsgType.CMD_SET_BW and self.bw_locked:
            msg = b"bw locked at 28000000 by wifi_fpga\0"
            self.send(MsgType.RESP_ERROR, bytes([mtype]) + msg)
            return
        if mtype == MsgType.CMD_SET_FREQ:
            self.freq = struct.unpack('<Q', payload)[0]
        elif mtype == MsgType.CMD_SET_BW:
            self.bw = struct.unpack('<I', payload)[0]
        elif mtype == MsgType.CMD_SET_FFT_SIZE:
            self.fft_size = struct.unpack('<I', payload)[0]
        elif mtype == MsgType.CMD_SET_GAIN:
            self.gain = struct.unpack('<f', payload)[0]
        elif mtype == MsgType.CMD_SET_AGC:
            self.agc = payload[0]
        elif mtype == MsgType.CMD_SET_MODE:
            self.mode = payload[0]
            self.policy = _policy_for_mode(self.mode, self.fpga)
        elif mtype == MsgType.CMD_SET_FPGA:
            self.fpga = bool(payload[0])
            self.policy = _policy_for_mode(self.mode, self.fpga)
        # New firmware: mutating command -> ack + status push to issuer
        self.send(MsgType.RESP_OK, bytes([mtype]))
        self.send(MsgType.RESP_STATUS, self.status_payload())

    def send(self, mtype, payload):
        if self._conn:
            try:
                self._conn.sendall(build_frame(mtype, payload))
            except OSError:
                pass

    def stop(self):
        try:
            if self._conn:
                self._conn.close()
        except OSError:
            pass
        self._srv.close()
        self.join(timeout=1)


def _make_daemon(config, fake, **kw):
    d = SheshaDaemon(config, host=fake.host, port=fake.port, **kw)
    return d


def _connect_and_poll(d, fake, settle=0.2):
    ok = d.connect()
    time.sleep(settle)
    d.poll()
    return ok


# ─── Lifecycle (mock subprocess) ─────────────────────────────────────────

def test_initial_state():
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp')
        assert d.connected is False
        assert d.busy is False
        assert d.get_state() == {}
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_sync_calls_stop_then_deploy(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        result = d.sync()
        assert result is True
        targets = [c[0][0] for c in mock_run.call_args_list
                   if isinstance(c[0][0], list) and c[0][0][0] == 'make']
        assert ['make', 'stop'] in targets
        assert ['make', 'deploy'] in targets
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_stop_calls_make_stop(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        result = d.stop()
        assert result is True
        call_args = mock_run.call_args
        assert 'stop' in call_args[0][0]
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_start_no_port_already_running(mock_run):
    """start() with no port and daemon running -> do nothing."""
    mock_run.return_value = MagicMock(returncode=0, stdout='1234\n', stderr='')
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        result = d.start()
        assert result is True
        assert d.busy is False
        assert mock_run.call_count == 1
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_start_no_port_not_running(mock_run):
    """start() with no daemon running -> launch it, mark fresh."""
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout='', stderr=''),  # pidof: not running
        MagicMock(returncode=0, stdout='', stderr=''),  # ssh launch
    ]
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        result = d.start()
        assert result is True
        assert d.busy is False
        assert d._fresh_start is True
        launch_cmd = mock_run.call_args_list[1][0][0]
        assert 'shesha-daemon 5555' in ' '.join(launch_cmd)
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_start_same_port_already_running(mock_run):
    """start(port=5555) when already on 5555 -> do nothing, not fresh."""
    mock_run.return_value = MagicMock(returncode=0, stdout='1234\n', stderr='')
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj', port=5555)
        result = d.start(port=5555)
        assert result is True
        assert mock_run.call_count == 1
        assert d._fresh_start is False
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_smart_connect_already_connected(mock_run):
    """smart_connect when already connected -> return immediately."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        d._client = MagicMock()
        d._client.connected = True
        d.connected = True
        d._host = '192.168.2.1'
        d._port = 5555
        result = d.smart_connect()
        assert 'already connected' in result
        assert mock_run.call_count == 0
    finally:
        os.unlink(path)


def test_fm_tx_stream_start_clears_stale_telemetry():
    """A new stream must not reuse the previous stream's emitted count."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)
        d._client = MagicMock()
        d._client.connected = True
        d._tx_stats = object()

        assert d.fm_tx_stream_start([0.5] * 100)
        assert d._tx_stats is None
    finally:
        os.unlink(path)


# ─── Wire-level: daemon is the source of truth ───────────────────────────

def test_connect_to_running_daemon_sends_no_config():
    """Reconnecting adopts the daemon's state; no config is pushed."""
    fake = FakeDaemon()
    fake.bw = 28_000_000
    fake.fpga = True
    fake.mode = 1
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        d.set_mode(1)  # saved app mode, pre-connect
        assert _connect_and_poll(d, fake)

        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SUBSCRIBE in sent
        assert MsgType.CMD_STATUS in sent
        for config_cmd in (MsgType.CMD_SET_FREQ, MsgType.CMD_SET_GAIN,
                           MsgType.CMD_SET_BW, MsgType.CMD_SET_FFT_SIZE,
                           MsgType.CMD_SET_MODE):
            assert config_cmd not in sent, \
                f"config command {config_cmd!r} sent to a running daemon"
    finally:
        fake.stop()
        os.unlink(path)


def test_connect_after_fresh_start_seeds_config():
    """A daemon we just started gets the config seed + saved mode."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config({'bw': '2500000', 'sample_rate': '4000000'})
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        d.set_mode(2)  # IQ, saved app mode
        assert _connect_and_poll(d, fake)

        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_MODE in sent
        assert MsgType.CMD_SET_FREQ in sent
        assert MsgType.CMD_SET_BW in sent
        assert MsgType.CMD_SET_GAIN in sent   # IQ does not pin AGC
        assert MsgType.CMD_SET_FPS in sent
        assert MsgType.CMD_SET_SAMPLE_RATE in sent
        assert MsgType.CMD_SUBSCRIBE in sent
        assert MsgType.CMD_STATUS in sent
        # Mode was sent with the seed (before subscribe/status)
        assert sent.index(MsgType.CMD_SET_MODE) < sent.index(MsgType.CMD_SUBSCRIBE)
    finally:
        fake.stop()
        os.unlink(path)


def test_connect_seeds_before_publishing_client():
    """The event loop must not see the socket while the seed reads status."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        seen = []
        d._seed_fresh = lambda client=None: seen.append(d._client)
        assert d.connect()
        assert seen and seen[0] is None
    finally:
        fake.stop()
        os.unlink(path)


def test_radio_policy_comes_from_status():
    fake = FakeDaemon()
    fake.mode = 2
    fake.policy = _policy_for_mode(2, False)
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        p = d.radio_policy()
        assert p is not None and p.sr_is_user_input
        assert p.allows_sr(4_000_000)
        assert not p.allows_sr(20_000_000)
    finally:
        fake.stop()
        os.unlink(path)


def test_set_mode_clears_stale_policy_until_status():
    """Switching producers drops the old policy until the new status lands.

    Otherwise a :set issued in the meantime is judged against the previous
    mode's rules — a false accept that errors, or a false reject that
    confuses.
    """
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        assert d.radio_policy() is not None          # waterfall policy

        d.set_mode(2)                                # -> IQ producer
        assert d.radio_policy() is None              # deferred to firmware

        deadline = time.monotonic() + 2.0
        while d.radio_policy() is None and time.monotonic() < deadline:
            time.sleep(0.02)
            d.poll()
        p = d.radio_policy()
        assert p is not None and p.sr_is_user_input  # IQ policy now live
        assert d.get_state()['mode'] == '2'
    finally:
        fake.stop()
        os.unlink(path)


def test_set_mode_same_producer_keeps_policy():
    """A redundant mode set must not blank the live policy."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        assert d.radio_policy() is not None
        d.set_mode(0)                                # already waterfall
        assert d.radio_policy() is not None
    finally:
        fake.stop()
        os.unlink(path)


def test_close_drops_policy():
    """The policy belongs to a link; closing the link must forget it."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        assert d.radio_policy() is not None
        d._close()
        assert d.radio_policy() is None
    finally:
        fake.stop()
        os.unlink(path)


def test_await_status_policy_bails_when_disconnected():
    """A dead link returns at once instead of burning the full timeout."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)
        d._client = MagicMock()
        d._client.connected = False
        start = time.monotonic()
        assert d._await_status_policy(d._client, timeout=5.0) is None
        assert time.monotonic() - start < 1.0
    finally:
        os.unlink(path)


def test_seed_skips_derived_sample_rate():
    """Waterfall derives sr from bw; replaying sr fights the mode."""
    fake = FakeDaemon()          # default waterfall policy
    fake.start()
    config, path = _make_config({'bw': '2500000', 'sample_rate': '2500000'})
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        assert _connect_and_poll(d, fake)
        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_BW in sent
        assert MsgType.CMD_SET_SAMPLE_RATE not in sent
    finally:
        fake.stop()
        os.unlink(path)


def test_seed_drops_out_of_range_iq_rate():
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config({'bw': '2500000', 'sample_rate': '20000000'})
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        d.set_mode(2)
        assert _connect_and_poll(d, fake)
        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_BW in sent
        assert MsgType.CMD_SET_SAMPLE_RATE not in sent
    finally:
        fake.stop()
        os.unlink(path)


def test_seed_sends_mode_before_radio_keys():
    """The saved mode must precede bw/sample_rate: the daemon validates
    those rates against the active mode's policy, so seeding an IQ-era
    rate while the daemon still sits in waterfall mode is rejected."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config({'sample_rate': '4000000', 'bw': '2500000'})
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        d.set_mode(2)  # const / IQ, saved pre-connect
        assert _connect_and_poll(d, fake)
        sent = [t for t, _ in fake.commands]
        assert sent.index(MsgType.CMD_SET_MODE) < sent.index(MsgType.CMD_SET_BW)
        assert sent.index(MsgType.CMD_SET_MODE) < sent.index(
            MsgType.CMD_SET_SAMPLE_RATE)
    finally:
        fake.stop()
        os.unlink(path)


def test_seed_skips_gain_when_mode_pins_agc():
    """wifi_fpga owns the AGC; a saved gain must not be replayed into it."""
    fake = FakeDaemon()
    fake.mode = 1
    fake.fpga = True
    fake.policy = _policy_for_mode(1, True)
    fake.start()
    config, path = _make_config({'gain': '30'})
    try:
        d = _make_daemon(config, fake)
        d._fresh_start = True
        assert _connect_and_poll(d, fake)
        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_GAIN not in sent
        assert MsgType.CMD_SET_FREQ in sent      # other keys still seed
    finally:
        fake.stop()
        os.unlink(path)


def test_get_state_returns_only_daemon_status():
    """State is the RESP_STATUS payload — never client intent."""
    fake = FakeDaemon()
    fake.bw = 28_000_000
    fake.fpga = True
    fake.mode = 1
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        state = d.get_state()
        assert state['bw'] == '28000000'
        assert state['fpga_active'] == '1'
        assert state['mode'] == '1'
        assert state['freq'] == '5180000000'
        assert state['iq_bits'] == '12'
        assert state['clip_count'] == '0'
        assert state['sample_count'] == '0'
    finally:
        fake.stop()
        os.unlink(path)


def test_adc_rail_counters_land_in_state():
    fake = FakeDaemon()
    fake.clip_count = 1234
    fake.sample_count = 50_000
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        state = d.get_state()
        assert state['clip_count'] == '1234'
        assert state['sample_count'] == '50000'
    finally:
        fake.stop()
        os.unlink(path)


def test_restart_adopts_daemon_truth():
    """The bug scenario: TUI restart against a daemon running wifi_fpga.

    Config says bw=20M, but the daemon holds bw locked at 28M by the
    fpga producer.  The client must display 28M / fpga on, and must not
    have re-applied the stale config to the wire.
    """
    fake = FakeDaemon()
    fake.bw = 28_000_000
    fake.fpga = True
    fake.mode = 1
    fake.bw_locked = True
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        d.set_mode(1)  # saved app mode, pre-connect
        assert _connect_and_poll(d, fake)

        state = d.get_state()
        assert state['bw'] == '28000000', \
            f"status bar would show {state.get('bw')} Hz instead of 28 MHz"
        assert state['fpga_active'] == '1'
        assert state['mode'] == '1'

        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_BW not in sent
        assert MsgType.CMD_SET_MODE not in sent
        # No stale error surfaced either
        assert d.last_error == ''
    finally:
        fake.stop()
        os.unlink(path)


def test_resp_error_sets_last_error_but_not_state():
    fake = FakeDaemon()
    fake.bw_locked = True
    fake.bw = 28_000_000
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        d.apply('bw', '20000000')
        time.sleep(0.2)
        d.poll()
        assert 'bw locked' in d.last_error
        state = d.get_state()
        assert state.get('bw', '28000000') == '28000000', \
            "failed command must not alter displayed state"
    finally:
        fake.stop()
        os.unlink(path)


def test_apply_sends_command_without_state_change():
    """apply() puts bytes on the wire; state only changes via RESP_STATUS."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        changes = []
        d.on_change(lambda k: changes.append(k))

        d.apply('freq', '5200000000')
        time.sleep(0.2)
        d.poll()

        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_FREQ in sent
        # State reflects the pushed status, which carried the new freq
        assert d.get_state()['freq'] == '5200000000'
        # Transient send feedback (pulse animation), not pending state
        assert '_sent' in changes
    finally:
        fake.stop()
        os.unlink(path)


def test_set_fpga_roundtrip():
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        d.set_fpga(True)
        time.sleep(0.2)
        d.poll()
        assert d.get_state()['fpga_active'] == '1'
        sent = [t for t, _ in fake.commands]
        assert MsgType.CMD_SET_FPGA in sent
    finally:
        fake.stop()
        os.unlink(path)


def test_set_iq_bits_pushes_without_reading_the_socket():
    """The event loop owns socket reads; a key handler must only send.

    A synchronous request from the keyboard thread races the event loop's
    drain and can corrupt the shared receive buffer.
    """
    class _Wire:
        connected = True

        def __init__(self):
            self.sent: list[bytes] = []
            self.blocking_reads = 0

        def send_raw(self, data: bytes) -> None:
            self.sent.append(data)

        def set_iq_bits(self, bits: int) -> bool:
            self.blocking_reads += 1
            return True

    config, path = _make_config()
    try:
        d = SheshaDaemon(config, host='127.0.0.1', port=1)
        wire = _Wire()
        d._client = wire
        assert d.set_iq_bits(8) is True
        assert wire.blocking_reads == 0
        assert wire.sent
        assert wire.sent[-1][0] == MsgType.CMD_SET_IQ_BITS
    finally:
        os.unlink(path)


def test_resp_ok_acks_pulse_key():
    """A successful SET_AGC acks the gain pulse even when gain_db
    doesn't change — the status push can't settle it (no 'gain' notify)."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        changes = []
        d.on_change(lambda k: changes.append(k))
        d.apply('gain', 'slow')
        time.sleep(0.2)
        d.poll()
        assert d.last_acked == 'gain'
        assert '_acked' in changes
    finally:
        fake.stop()
        os.unlink(path)


def test_resp_error_acks_pulse_key():
    """A rejected SET_AGC still settles the pulse (error shown, pulse stops)."""
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert _connect_and_poll(d, fake)
        # Turn on the bw lock so CMD_SET_BW is rejected
        fake.bw_locked = True
        d.apply('bw', '40000000')
        time.sleep(0.2)
        d.poll()
        assert d.last_acked == 'bw'
        assert 'bw locked' in d.last_error
    finally:
        fake.stop()
        os.unlink(path)


# ─── Activity / liveness model ───────────────────────────────────────────

def test_doing_is_nested_safe():
    """An inner operation must not clear the outer one's activity."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)
        with d._doing('connect'):
            assert d.busy and d.activity == 'connect'
            with d._doing('deploy'):
                assert d.busy and d.activity == 'deploy'
            assert d.busy and d.activity == 'connect'
        assert not d.busy and d.activity is None
    finally:
        os.unlink(path)


@patch('shesha.tui.daemon.subprocess.run')
def test_smart_connect_flashes_from_the_first_probe(mock_run):
    """Every step, including the pre-flight SSH probes, is activity."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, project_dir='/tmp/proj')
        busy_during = []

        def fake(*args, **kwargs):
            busy_during.append(d.busy)
            return MagicMock(returncode=1, stdout='', stderr='')

        mock_run.side_effect = fake
        with pytest.raises(RuntimeError):
            d.smart_connect()
        assert busy_during and all(busy_during)
    finally:
        os.unlink(path)


def test_check_alive_closes_on_silence():
    """Liveness keys off bytes actually received, not off polling."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)

        class FakeClient:
            def __init__(self, last_rx):
                self.last_rx = last_rx

            def disconnect(self):
                pass

        d.connected = True
        d._client = FakeClient(time.time() - 10.0)
        assert d.check_alive(timeout=1.0) is False
        assert d.connected is False
        assert d._client is None
    finally:
        os.unlink(path)


def test_check_alive_grants_grace_while_a_mutation_is_pending():
    """A mode switch can block the daemon past the idle heartbeat timeout."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)

        class FakeClient:
            def __init__(self, last_rx):
                self.last_rx = last_rx

            def disconnect(self):
                pass

        d.connected = True
        d._client = FakeClient(time.time() - 2.0)
        d._sent('mode')
        assert d.check_alive(timeout=1.0, grace=5.0) is True
        assert d.connected is True
    finally:
        os.unlink(path)


def test_check_alive_drops_after_the_grace_expires():
    """An unanswered mutation must not keep a dead link alive forever."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)

        class FakeClient:
            def __init__(self, last_rx):
                self.last_rx = last_rx

            def disconnect(self):
                pass

        d.connected = True
        d._client = FakeClient(time.time() - 6.0)
        d._sent('mode')
        assert d.check_alive(timeout=1.0, grace=5.0) is False
        assert d.connected is False
    finally:
        os.unlink(path)


def test_check_alive_restores_the_idle_timeout_once_answered():
    """An ack ends the grace window; silence is then fatal again."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)

        class FakeClient:
            def __init__(self, last_rx):
                self.last_rx = last_rx

            def disconnect(self):
                pass

        d.connected = True
        d._client = FakeClient(time.time() - 2.0)
        d._sent('mode')
        d._dispatch(MsgType.RESP_OK, bytes([MsgType.CMD_SET_MODE]), [])
        assert d.check_alive(timeout=1.0, grace=5.0) is False
        assert d.connected is False
    finally:
        os.unlink(path)


def test_notifications_wait_for_the_ui_thread():
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, host='127.0.0.1', port=1)
        d.set_ui_thread(threading.get_ident())
        seen = []
        d.on_change(seen.append)

        thread = threading.Thread(target=lambda: d._notify('mode'))
        thread.start()
        thread.join()
        assert seen == []

        d.drain_notifications()
        assert seen == ['mode']

        d._notify('gain')
        assert seen == ['mode']
        d.drain_notifications()
        assert seen == ['mode', 'gain']
    finally:
        os.unlink(path)


def test_poll_survives_the_client_disappearing_mid_call():
    """A close between the check and the use must not raise."""
    config, path = _make_config()
    try:
        d = SheshaDaemon(config)

        class RacingClient:
            connected = True

            def recv_raw(self, timeout=0):
                d._client = None
                return []

            def disconnect(self):
                pass

        d._client = RacingClient()
        assert d.poll() == []
    finally:
        os.unlink(path)


def test_daemon_carries_link_seed_state():
    config, path = _make_config()
    try:
        d = SheshaDaemon(config, host='10.0.0.2', port=6000,
                         fresh_start=True, desired_mode='3')
        assert d.host == '10.0.0.2'
        assert d.port == 6000
        assert d.fresh_start is True
        assert d.desired_mode == '3'
    finally:
        os.unlink(path)


def test_status_replaces_the_state_snapshot_atomically():
    fake = FakeDaemon()
    fake.start()
    config, path = _make_config()
    try:
        d = _make_daemon(config, fake)
        assert d.connect()
        old = d._state
        time.sleep(0.2)
        d.poll()
        assert d._state is not old
    finally:
        fake.stop()
        os.unlink(path)
