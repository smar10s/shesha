"""Reusable TCP client for the shesha daemon protocol.

Provides DaemonClient for connecting to the daemon, sending commands,
and receiving streaming data frames. No CLI logic — see the shesha.cli package for that.
"""

from __future__ import annotations

import selectors
import socket
import struct
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator

from shesha.protocol import (
    FRAME_HEADER,
    MsgType,
    DaemonMode,
    Status,
    Capabilities,
    PsdFrame,
    WifiFrame,
    WifiDiag,
    FmFrame,
    IqfFrame,
    TxStats,
    ThreadStats,
    cmd_status,
    cmd_caps,
    cmd_set_mode,
    cmd_set_fpga,
    cmd_set_freq,
    cmd_set_bw,
    cmd_set_gain,
    cmd_set_agc,
    cmd_set_fps,
    cmd_set_fft_size,
    cmd_set_window,
    cmd_set_sample_rate,
    cmd_set_accum,
    cmd_set_workers,
    cmd_wifi_diag,
    cmd_tx_wifi,
    cmd_tx_fm,
    cmd_tx_fm_stream_start,
    cmd_set_rds,
    cmd_tx_stream_data,
    cmd_tx_stream_end,
    cmd_tx_stream_flush,
    cmd_tx_stats,
    cmd_thread_stats,
    cmd_tx_stop,
    TX_STOP_ALL,
    cmd_set_tx_atten,
    cmd_subscribe,
    cmd_set_iq_bits,
    cmd_iq_tx_stream_data,
    cmd_iq_tx_stream_end,
    SUBSCRIBE_PSD,
    SUBSCRIBE_WIFI,
    SUBSCRIBE_FM,
    SUBSCRIBE_IQ,
    SUBSCRIBE_ALL,
    FrameDecodeError,
    parse_status,
    parse_caps,
    parse_psd,
    parse_wifi,
    parse_wifi_diag,
    parse_fm,
    parse_iq,
    parse_tx_stats,
    parse_thread_stats,
    build_frame,
    MAX_PAYLOAD,
    TX_PROTO_NAMES,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class SubscriptionError(RuntimeError):
    """The daemon rejected a data subscription.

    Carries the daemon's reason, for example a stream that is busy with
    an active transmission.
    """


class PushResult(IntEnum):
    """Outcome of a streaming-audio push.

    RETRY is not a failure.  The daemon's TX audio ring is bounded, so it
    is the normal signal that the client has run ahead of playout and
    should wait before re-sending the same chunk.
    """
    OK = 0
    RETRY = 1
    ERROR = 2


MODE_NAMES: dict[int, str] = {
    DaemonMode.WATERFALL: "waterfall",
    DaemonMode.WIFI: "wifi",
    DaemonMode.IQ: "iq",
    DaemonMode.FM: "fm",
}

MODE_BY_NAME: dict[str, DaemonMode] = {v: k for k, v in MODE_NAMES.items()}


def _parse_status_full(payload: bytes) -> tuple[Status, int]:
    status = parse_status(payload)
    return status, status.daemon_mode


def _status_to_dict(status: Status, mode: int) -> dict:
    return {
        "mode": mode,
        "mode_name": MODE_NAMES.get(mode, "unknown"),
        "freq_hz": status.freq_hz,
        "freq_mhz": status.freq_hz / 1e6,
        "sample_rate_hz": status.sample_rate_hz,
        "sample_rate_mhz": status.sample_rate_hz / 1e6,
        "bw_hz": status.bw_hz,
        "bw_mhz": status.bw_hz / 1e6,
        "gain_db": status.gain_db,
        "agc_mode": status.agc_mode,
        "fft_size": status.fft_size,
        "target_fps": status.target_fps,
        "window_type": status.window_type,
        "tx_atten_db": status.tx_atten_db,
        "tx_busy": bool(status.tx_busy),
        "tx_proto": status.tx_proto,
        "tx_proto_name": TX_PROTO_NAMES.get(status.tx_proto, "unknown"),
        "tx_flags": status.tx_flags,
        "tx_repeat": status.tx_repeat,
        "tx_interval_us": status.tx_interval_us,
        "fpga_active": status.fpga_active,
        "fpga_running": status.fpga_running,
        "rx_active": status.rx_active,
        "iq_bits": status.iq_bits,
        "psd_coverage": status.psd_coverage,
        "workers": status.workers,
        "clip_count": status.clip_count,
        "sample_count": status.sample_count,
    }


def _caps_to_dict(caps: Capabilities) -> dict:
    features = []
    if caps.features & 1:
        features.append("waterfall")
    if caps.features & 2:
        features.append("wifi decode")
    if caps.features & 4:
        features.append("fabric wifi")
    if caps.features & 8:
        features.append("iq")
    return {
        "fpga_build_id": caps.fpga_build_id,
        "features": caps.features,
        "feature_names": features,
        "features_waterfall": bool(caps.features & 1),
        "features_wifi_decode": bool(caps.features & 2),
        "features_fabric_wifi": bool(caps.features & 4),
        "features_iq": bool(caps.features & 8),
        "max_sample_rate": caps.max_sample_rate,
        "max_fft_size": caps.max_fft_size,
        "min_freq_mhz": caps.min_freq_mhz,
        "max_freq_mhz": caps.max_freq_mhz,
        "fw_build_id": caps.fw_build_id,
    }


def _psd_bin_stats(frame: PsdFrame) -> dict:
    bins = frame.bins
    if not bins:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(bins),
        "min": min(bins),
        "max": max(bins),
        "mean": sum(bins) / len(bins),
    }


# ---------------------------------------------------------------------------
# StreamResult
# ---------------------------------------------------------------------------


@dataclass
class PsdAggregate:
    """Running PSD summary that does not grow with capture length."""

    frames: int = 0
    bin_count: int = 0
    power_sum: float = 0.0
    peak_db: float | None = None
    peak_bin: int = 0
    peak_center_mhz: float = 0.0
    last_center_mhz: float = 0.0

    def add(self, frame: PsdFrame) -> None:
        if self.frames == 0:
            self.bin_count = len(frame.bins)
        self.frames += 1
        self.last_center_mhz = frame.center_freq_mhz
        for index, power in enumerate(frame.bins):
            self.power_sum += power
            if self.peak_db is None or power > self.peak_db:
                self.peak_db = power
                self.peak_bin = index
                self.peak_center_mhz = frame.center_freq_mhz

    @property
    def mean_db(self) -> float | None:
        count = self.frames * self.bin_count
        return self.power_sum / count if count else None


@dataclass
class StreamResult:
    duration: float
    mode: int
    psd_frames: list[PsdFrame] = field(default_factory=list)
    wifi_frames: list[WifiFrame] = field(default_factory=list)
    fm_frames: list[FmFrame] = field(default_factory=list)
    iq_frames: list[IqfFrame] = field(default_factory=list)
    status_updates: list[Status] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    psd: PsdAggregate = field(default_factory=PsdAggregate)
    wifi_seen: int = 0
    fm_seen: int = 0
    iq_seen: int = 0
    wifi_fcs_ok: int = 0

    @property
    def psd_count(self) -> int:
        return self.psd.frames

    @property
    def wifi_count(self) -> int:
        return self.wifi_seen

    @property
    def fm_count(self) -> int:
        return self.fm_seen

    @property
    def iq_count(self) -> int:
        return self.iq_seen

    @property
    def psd_fps(self) -> float:
        return self.psd_count / self.duration if self.duration > 0 else 0.0

    @property
    def wifi_fps(self) -> float:
        return self.wifi_count / self.duration if self.duration > 0 else 0.0

    def to_dict(self, verbose: bool = False) -> dict:
        d: dict = {
            "duration": self.duration,
            "mode": self.mode,
            "mode_name": MODE_NAMES.get(self.mode, "unknown"),
            "psd_count": self.psd_count,
            "wifi_count": self.wifi_count,
            "wifi_fcs_ok": self.wifi_fcs_ok,
            "psd_fps": self.psd_fps,
            "wifi_fps": self.wifi_fps,
            "fm_count": self.fm_count,
            "iq_count": self.iq_count,
            "errors": self.errors,
        }
        if self.status_updates:
            d["status_updates"] = [
                {"daemon_mode": s.daemon_mode, "sample_rate_hz": s.sample_rate_hz,
                 "freq_hz": s.freq_hz, "rx_active": s.rx_active}
                for s in self.status_updates
            ]
        if verbose:
            d["psd_per_frame"] = [_psd_bin_stats(f) for f in self.psd_frames]
            d["wifi_per_frame"] = [
                {
                    "ts": f.timestamp,
                    "rate_mbps": f.rate_mbps,
                    "length": f.length,
                    "fcs_ok": f.fcs_ok,
                    "psdu_len": len(f.psdu),
                }
                for f in self.wifi_frames
            ]
        return d


# ---------------------------------------------------------------------------
# DaemonClient
# ---------------------------------------------------------------------------


# A frame far larger than anything the daemon emits means the stream has
# desynced.  Disconnect rather than buffer until memory runs out.
_MAX_FRAME_PAYLOAD = MAX_PAYLOAD
_MAX_BUFFER_BYTES = _MAX_FRAME_PAYLOAD + (1 << 16)
# A wedged daemon must not block the TUI forever in sendall().
_SEND_TIMEOUT = 2.0


class DaemonClient:
    """Lightweight TCP client for the shesha daemon protocol.

    Opens a socket, sends TLV command frames, parses responses and streaming
    data frames.  No Config, no subprocess, no pending/confirm state machine.

    Reads and writes are serialized internally.  Reads must still be owned
    by one thread: a second reader would consume replies the first expects.
    """

    def __init__(self, host: str = "192.168.2.1", port: int = 5555,
                 timeout: float = 5.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b''
        self._sel: selectors.BaseSelector | None = selectors.DefaultSelector()
        self._send_lock = threading.Lock()
        self._recv_lock = threading.RLock()
        self._last_seq: int | None = None
        self._gaps: int = 0
        self._dropped: int = 0
        self._psd_frame_index: int | None = None
        # Wall-clock time of the last byte actually received.  A liveness
        # check must key off this, not off "the socket was polled": an
        # empty non-blocking read proves nothing about the peer.
        self.last_rx: float = 0.0
        # Reason from the last rejected config command, for CLI messages.
        self.last_error: str | None = None

    # -- connection ----------------------------------------------------------

    def connect(self) -> None:
        if self._sock:
            return
        if self._sel is None:
            self._sel = selectors.DefaultSelector()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._timeout)
            sock.connect((self._host, self._port))
            sock.settimeout(0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sel.register(sock, selectors.EVENT_READ)
        except Exception:
            sock.close()
            if self._sel is not None:
                self._sel.close()
                self._sel = None
            raise
        self._sock = sock
        self._buf = b''
        self._last_seq = None
        self._gaps = 0
        self._dropped = 0
        self._psd_frame_index = None
        self.last_rx = time.time()

    def disconnect(self) -> None:
        with self._send_lock:
            sock = self._sock
            self._sock = None
        with self._recv_lock:
            if sock and self._sel is not None:
                try:
                    self._sel.unregister(sock)
                except (KeyError, ValueError, OSError):
                    pass
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self._buf = b''
            selector = self._sel
            self._sel = None
            if selector is not None:
                selector.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    @property
    def gaps(self) -> int:
        """Number of gap events (discontinuities in seq) since connect.

        Excludes PSD, whose seq is a producer frame index rather than a
        delivery counter — see _parse_frames.
        """
        return self._gaps

    @property
    def dropped(self) -> int:
        """Total frames dropped (sum of all gap sizes) since connect.

        Excludes PSD, for the same reason as `gaps`.
        """
        return self._dropped

    @property
    def psd_frame_index(self) -> int | None:
        """Stream position of the most recent PSD frame, or None.

        Counts frame-sized blocks of the sample stream, NOT frames the
        producer transformed: when the producer falls behind it abandons
        blocks, and the index advances over them.  So its slope is the ADC's
        rate divided by the FFT size, and it is the same whether coverage is
        100% or 25%.

        That makes it the right key for ordering frames and for comparing
        frames from two producers, and the wrong one for computing coverage.
        Coverage needs the count of blocks actually transformed, which the
        producer tracks separately.
        """
        return self._psd_frame_index

    def fileno(self) -> int:
        """Return the underlying socket fd for use with select/poll/epoll.

        Raises ValueError if not connected.  This lets external event loops
        (e.g. a TUI main loop) poll the daemon socket alongside other fds.
        """
        if not self._sock:
            raise ValueError("not connected")
        return self._sock.fileno()

    # -- raw I/O -------------------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        with self._send_lock:
            sock = self._sock
            if not sock:
                raise ConnectionError("not connected")
            orig_timeout = sock.gettimeout()
            sock.settimeout(_SEND_TIMEOUT)
            try:
                sock.sendall(data)
            finally:
                sock.settimeout(orig_timeout)

    def recv_raw(self, timeout: float | None = None) -> list[tuple[int, bytes]]:
        """Read from socket and parse complete TLV frames.

        Returns list of (msg_type, payload) tuples.  May be empty.
        """
        with self._recv_lock:
            sock = self._sock
            if not sock:
                return []

            if timeout is None:
                timeout = 0.1

            ready = self._sel.select(timeout=timeout)

            for _key, _mask in ready:
                try:
                    data = sock.recv(65536)
                except BlockingIOError:
                    break
                except OSError:
                    self.disconnect()
                    return []
                if not data:
                    self.disconnect()
                    return []
                self._buf += data
                self.last_rx = time.time()

            return self._parse_frames()

    def _parse_frames(self) -> list[tuple[int, bytes]]:
        frames: list[tuple[int, bytes]] = []
        hdr_size = FRAME_HEADER.size
        while len(self._buf) >= hdr_size:
            msg_type, payload_len, seq = FRAME_HEADER.unpack_from(self._buf, 0)
            if payload_len > _MAX_FRAME_PAYLOAD:
                self.disconnect()
                return frames
            frame_size = hdr_size + payload_len
            if len(self._buf) < frame_size:
                break
            payload = self._buf[hdr_size:frame_size]
            self._buf = self._buf[frame_size:]
            # Gap detection: for most data types seq is a monotonic counter on
            # the daemon's per-client outbound ring, so a jump > 1 means
            # frames were dropped because this client couldn't keep up.
            #
            # DATA_PSD is excluded because its seq is not a delivery counter.
            # It carries the producer's frame index, and the producer computes
            # many frames per broadcast (~27 at 20 MSPS) which accumulation
            # reduces to one.  The index therefore jumps by design on every
            # healthy frame, and counting that as loss reported thousands of
            # dropped frames in normal operation.
            if seq != 0 and msg_type != MsgType.DATA_PSD:
                if self._last_seq is not None and seq > self._last_seq + 1:
                    gap = seq - self._last_seq - 1
                    self._gaps += 1
                    self._dropped += gap
                self._last_seq = seq
            elif msg_type == MsgType.DATA_PSD:
                self._psd_frame_index = seq
            frames.append((msg_type, bytes(payload)))
        if len(self._buf) > _MAX_BUFFER_BYTES:
            self.disconnect()
        return frames

    def wait(self, duration: float) -> list[tuple[int, bytes]]:
        """Sleep for `duration` while keeping the socket drained.

        Never use `time.sleep` between two requests on a connected client.
        The daemon broadcasts DATA_PSD at ~750 KiB/s, which fills the
        receive window in ~0.17 s; TCP then stops acking and the daemon's
        `send_all` evicts the client after 200 EAGAIN retries (~100 ms).
        Measured on hardware: pauses of 0.5 s survive, 1.0 s never does.

        This is why the old CPU-polling command never worked with its default
        `--interval 2.0` — it must sample twice and difference, and it slept
        in between.

        Returns the frames drained during the wait so a caller polling for
        broadcast data does not lose them.
        """
        frames: list[tuple[int, bytes]] = []
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._sock:
                # Nothing left to drain; honour the requested duration so
                # callers keep their measurement window.
                time.sleep(remaining)
                break
            frames.extend(self.recv_raw(timeout=min(remaining, 0.1)))
        return frames

    # -- request-response ----------------------------------------------------

    def _request_response(self, request: bytes, expected_type: int,
                          timeout: float = 2.0,
                          also: tuple[int, ...] = ()) -> tuple[int, bytes] | None:
        """Send a request frame, then poll for a matching response type.

        Returns (msg_type, payload) on match, or None on timeout/error.
        RESP_ERROR frames are also returned for the caller to inspect, as
        are any types listed in `also`.
        """
        self.last_error = None
        self.send_raw(request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A closed socket makes recv_raw return instantly, which would
            # turn this into a busy-spin until the deadline (measured 16.5M
            # iterations in 3 s on hardware).  Nothing more can arrive.
            if not self._sock:
                return None
            remaining = deadline - time.monotonic()
            for msg_type, payload in self.recv_raw(timeout=min(remaining, 0.1)):
                if (msg_type == expected_type
                        or msg_type == MsgType.RESP_ERROR
                        or msg_type in also):
                    if msg_type == MsgType.RESP_ERROR:
                        self.last_error = payload[1:].decode(
                            "utf-8", "replace").rstrip("\x00")
                    return (msg_type, payload)
                # Data frames arriving mid-request are dropped.
                # _parse_frames already consumed them from _buf; no
                # secondary queue exists.  Acceptable today: callers
                # only issue requests during TX-only (no inbound data)
                # or from the TUI which drains via wait().
        return None

    def _send_config(self, request: bytes, label: str,
                     timeout: float = 2.0) -> bool:
        """Send a config command and wait for RESP_OK.  Returns True on ok."""
        self.last_error = None
        result = self._request_response(request, MsgType.RESP_OK, timeout=timeout)
        if result is None:
            self.last_error = "no response from daemon"
            return False
        msg_type, payload = result
        if msg_type == MsgType.RESP_ERROR:
            self.last_error = payload[1:].decode(
                "utf-8", "replace").rstrip("\x00")
            return False
        return True

    # -- high-level commands -------------------------------------------------

    def status(self) -> tuple[Status, int] | None:
        result = self._request_response(cmd_status(), MsgType.RESP_STATUS)
        if result is None:
            return None
        msg_type, payload = result
        if msg_type == MsgType.RESP_ERROR:
            return None
        try:
            return _parse_status_full(payload)
        except (struct.error, FrameDecodeError) as exc:
            self.last_error = f"bad status frame: {exc}"
            return None

    def caps(self) -> Capabilities | None:
        result = self._request_response(cmd_caps(), MsgType.RESP_CAPS)
        if result is None:
            return None
        msg_type, payload = result
        if msg_type == MsgType.RESP_ERROR:
            return None
        try:
            return parse_caps(payload)
        except (struct.error, FrameDecodeError) as exc:
            self.last_error = f"bad caps frame: {exc}"
            return None

    def set_mode(self, mode: DaemonMode) -> bool:
        return self._send_config(cmd_set_mode(mode), "mode")

    def subscribe(self, mask: int = SUBSCRIBE_ALL) -> bool:
        """Subscribe to data streams.  Default subscribes to all."""
        return self._send_config(cmd_subscribe(mask), "subscribe")

    def set_fpga(self, enable: bool) -> bool:
        return self._send_config(cmd_set_fpga(enable), "fpga")

    def set_freq(self, freq_hz: int) -> bool:
        return self._send_config(cmd_set_freq(freq_hz), "freq")

    def set_bw(self, bw_hz: int) -> bool:
        return self._send_config(cmd_set_bw(bw_hz), "bw")

    def set_gain(self, gain_db: float) -> bool:
        return self._send_config(cmd_set_gain(gain_db), "gain")

    def set_agc(self, mode: int) -> bool:
        return self._send_config(cmd_set_agc(mode), "agc")

    def set_fps(self, fps: int) -> bool:
        return self._send_config(cmd_set_fps(fps), "fps")

    def set_fft_size(self, n: int) -> bool:
        return self._send_config(cmd_set_fft_size(n), "fft_size")

    def set_window(self, win: int) -> bool:
        return self._send_config(cmd_set_window(win), "window")

    def set_sample_rate(self, rate_hz: int) -> bool:
        return self._send_config(cmd_set_sample_rate(rate_hz), "sample_rate")

    def set_accum(self, mode: int) -> bool:
        return self._send_config(cmd_set_accum(mode), "accum")

    def set_workers(self, n: int) -> bool:
        """Request the PSD producer worker count (1 or 2).  The daemon clamps
        it to 1 at or below fs_max and reports the live count in status."""
        return self._send_config(cmd_set_workers(n), "workers")

    def set_iq_bits(self, bits: int) -> bool:
        """Set the IQ wire sample width (8 or 12), both directions."""
        return self._send_config(cmd_set_iq_bits(bits), "iq_bits")

    def set_tx_atten(self, db: float) -> bool:
        return self._send_config(cmd_set_tx_atten(db), "tx_atten")

    def wifi_tx(self, rate_mbps: int, psdu: bytes,
                repeat: int = 1, interval_us: int = 0,
                cyclic: bool = False) -> bool:
        return self._send_config(
            cmd_tx_wifi(rate_mbps, psdu, repeat, interval_us, cyclic),
            "tx")

    def tx_stop(self, target: int = TX_STOP_ALL) -> bool:
        return self._send_config(cmd_tx_stop(target), "tx_stop")

    def fm_tx(self, audio: list[float], *,
              audio_rate: int = 44100,
              deviation: int = 75000,
              dac_rate: int = 2_100_000,
              rds=None) -> bool:
        return self._send_config(
            cmd_tx_fm(audio, audio_rate=audio_rate,
                      deviation=deviation, dac_rate=dac_rate, rds=rds),
            "fm_tx", timeout=10.0)

    def fm_tx_stream_start(self, audio: Sequence[float], *,
                           audio_rate: int = 44100,
                           deviation: int = 75000,
                           dac_rate: int = 2_100_000,
                           rds=None) -> bool:
        return self._send_config(
            cmd_tx_fm_stream_start(audio, audio_rate=audio_rate,
                                   deviation=deviation, dac_rate=dac_rate,
                                   rds=rds),
            "fm_stream_start", timeout=10.0)

    def set_rds(self, rds) -> bool:
        """Update the RDS fields of an active FM stream (CMD_SET_RDS)."""
        return self._send_config(cmd_set_rds(rds), "set_rds")

    def fm_tx_stream_push(self, audio: Sequence[float],
                          timeout: float = 10.0) -> PushResult:
        """Append audio to an active FM stream.

        Tri-state on purpose.  The daemon buffers audio in a bounded ring,
        so RETRY means "you are ahead of realtime, wait and re-send this
        same chunk" — treating it as an error would abandon a healthy
        stream the moment the client outran playout.
        """
        result = self._request_response(
            cmd_tx_stream_data(audio), MsgType.RESP_OK,
            timeout=timeout, also=(MsgType.RESP_RETRY,))
        if result is None:
            return PushResult.ERROR
        msg_type, _payload = result
        if msg_type == MsgType.RESP_RETRY:
            return PushResult.RETRY
        if msg_type == MsgType.RESP_ERROR:
            return PushResult.ERROR
        return PushResult.OK

    def fm_tx_stream_end(self) -> bool:
        return self._send_config(
            cmd_tx_stream_end(),
            "fm_stream_end", timeout=5.0)

    def fm_tx_stream_flush(self) -> bool:
        """Discard the queued, not-yet-modulated audio of an FM stream.

        Used to seek without tearing the carrier down: the stream keeps
        running, but the audio it has buffered is dropped so the next push
        is heard almost immediately.
        """
        return self._send_config(
            cmd_tx_stream_flush(), "fm_stream_flush", timeout=5.0)

    def iq_tx_stream_data(self, data: bytes,
                          timeout: float = 10.0) -> PushResult:
        """Push raw packed IQ to the TX stream.  Implicitly starts the
        stream on first call.  RETRY means "ahead of realtime — wait and
        resend this same chunk"."""
        result = self._request_response(
            cmd_iq_tx_stream_data(data), MsgType.RESP_OK,
            timeout=timeout, also=(MsgType.RESP_RETRY,))
        if result is None:
            return PushResult.ERROR
        msg_type, _payload = result
        if msg_type == MsgType.RESP_RETRY:
            return PushResult.RETRY
        if msg_type == MsgType.RESP_ERROR:
            return PushResult.ERROR
        return PushResult.OK

    def iq_tx_stream_end(self) -> bool:
        return self._send_config(
            cmd_iq_tx_stream_end(), "iq_stream_end", timeout=5.0)

    def tx_stats(self) -> TxStats | None:
        """Streaming-TX telemetry straight off the FPGA counters."""
        result = self._request_response(cmd_tx_stats(), MsgType.RESP_TX_STATS)
        if result is None:
            return None
        msg_type, payload = result
        if msg_type == MsgType.RESP_ERROR:
            return None
        return parse_tx_stats(payload)

    def thread_stats(self) -> ThreadStats | None:
        """Per-thread CPU counters, cumulative.

        Two samples differenced give the CPU share over the interval
        between them; a single sample on its own says almost nothing.  See
        `ThreadStats.delta`.
        """
        result = self._request_response(cmd_thread_stats(),
                                        MsgType.RESP_THREAD_STATS)
        if result is None:
            return None
        msg_type, payload = result
        if msg_type == MsgType.RESP_ERROR:
            return None
        return parse_thread_stats(payload)

    def diag(self) -> WifiDiag | None:
        result = self._request_response(cmd_wifi_diag(), MsgType.RESP_WIFI_DIAG)
        if result is None:
            return None
        _msg_type, payload = result
        if _msg_type == MsgType.RESP_ERROR:
            return None
        return parse_wifi_diag(payload)

    # -- streaming -----------------------------------------------------------

    def stream(self, duration: float = 5.0, verbose: bool = False,
               psd_sink: Callable[[PsdFrame], None] | None = None,
               wifi_sink: Callable[[WifiFrame], None] | None = None,
               fm_sink: Callable[[FmFrame], None] | None = None,
               iq_sink: Callable[[IqfFrame], None] | None = None
               ) -> StreamResult:
        """Receive frames for `duration` seconds.  Does not change mode.

        Auto-subscribes to all data types if not already subscribed.
        Frames are counted as they arrive but not retained; `verbose`
        keeps each one in the result.  A `*_sink` callable receives frames
        one at a time for callers that consume a capture without buffering
        it.  PSD frames are always aggregated.
        """
        result = StreamResult(duration=duration, mode=-1)
        if not self.subscribe(SUBSCRIBE_ALL):
            reason = self.last_error or "rejected"
            result.errors.append(f"subscribe failed: {reason}")
            return result
        start = time.monotonic()

        while time.monotonic() - start < duration:
            # A closed socket makes recv_raw return instantly, turning this
            # into a busy-spin until the deadline.  Nothing more can arrive.
            if not self.connected:
                break
            remaining = duration - (time.monotonic() - start)
            for msg_type, payload in self.recv_raw(timeout=min(remaining, 0.25)):
                if msg_type == MsgType.DATA_PSD:
                    try:
                        frame = parse_psd(payload)
                    except Exception as exc:
                        result.errors.append(f"bad PSD frame: {exc}")
                    else:
                        result.psd.add(frame)
                        if verbose:
                            result.psd_frames.append(frame)
                        if psd_sink is not None:
                            psd_sink(frame)
                elif msg_type == MsgType.DATA_WIFI:
                    try:
                        frame = parse_wifi(payload)
                    except Exception as exc:
                        result.errors.append(f"bad WiFi frame: {exc}")
                    else:
                        result.wifi_seen += 1
                        if frame.fcs_ok:
                            result.wifi_fcs_ok += 1
                        if verbose:
                            result.wifi_frames.append(frame)
                        if wifi_sink is not None:
                            wifi_sink(frame)
                elif msg_type == MsgType.DATA_FM:
                    try:
                        frame = parse_fm(payload)
                    except Exception as exc:
                        result.errors.append(f"bad FM frame: {exc}")
                    else:
                        result.fm_seen += 1
                        if verbose:
                            result.fm_frames.append(frame)
                        if fm_sink is not None:
                            fm_sink(frame)
                elif msg_type == MsgType.DATA_IQ:
                    try:
                        frame = parse_iq(payload)
                    except Exception as exc:
                        result.errors.append(f"bad IQ frame: {exc}")
                    else:
                        result.iq_seen += 1
                        if verbose:
                            result.iq_frames.append(frame)
                        if iq_sink is not None:
                            iq_sink(frame)
                elif msg_type == MsgType.RESP_STATUS:
                    try:
                        status, mode = _parse_status_full(payload)
                        result.status_updates.append(status)
                        if result.mode == -1:
                            result.mode = mode
                    except Exception as exc:
                        result.errors.append(f"bad status frame: {exc}")

        return result

    def iq_rx(self, duration: float = 0.0) -> Iterator[IqfFrame]:
        """Yield DATA_IQ frames as parsed IqfFrame objects.

        Subscribes to the IQ stream on entry.  duration <= 0 runs until
        the caller stops iterating; non-IQ frames are consumed and
        ignored.  Overflow accounting is available via .gaps/.dropped on
        the client.
        """
        if not self.subscribe(SUBSCRIBE_IQ):
            raise SubscriptionError(self.last_error or "subscription rejected")
        start = time.monotonic()
        while True:
            # An unbounded capture must end at EOF rather than spin on a
            # socket that can no longer deliver frames.
            if not self.connected:
                return
            if duration and time.monotonic() - start >= duration:
                return
            for msg_type, payload in self.recv_raw(timeout=0.1):
                if msg_type == MsgType.DATA_IQ:
                    try:
                        frame = parse_iq(payload)
                    except FrameDecodeError as exc:
                        raise FrameDecodeError(
                            f"bad IQ frame: {exc}") from exc
                    yield frame


