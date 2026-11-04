"""Headless view renderer for reviewing and testing TUI appearance.

Renders a view into an off-screen buffer using synthetic data, then
reports both the raw escape stream (for a human to ``cat`` and look at)
and a per-cell styled grid (for assertions).

This closes the verification loop on styling work: colour assignment and
layout are checked programmatically, and only aesthetics need human eyes.

Wide characters (CJK, emoji) occupy two grid cells; the continuation cell
is left empty so the plain grid still reads as the text a terminal shows.

    python3 -m shesha.tui.snapshot fm:rx
    python3 -m shesha.tui.snapshot wifi:scan --theme tokyonight -o /tmp/w.ans

``--shell`` adds the terminal chrome (status, TX and input bars) around
the view, and ``--state`` selects a synthesized view preset::

    python3 -m shesha.tui.snapshot fm:tx --state streaming --shell \\
        --width 80 --height 24 -o fm_tx.ans
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import io
import itertools
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from .pytui import ANSI_SEQUENCE, StyledWindow, char_width
from .theme import STYLES, DEFAULT_STYLE
from .registry import APP_CLASSES
from ..protocol import (
    RdsState, TxStats, TX_PROTO_FM, TX_PROTO_NONE, TX_PROTO_WIFI,
)

# Sample count for the synthetic waveform.  Enough to fill the widest
# supported terminal after downsampling.
_AUDIO_N = 2000

# One simulated frame at 60 fps.  Views redraw on every tick regardless
# of how much time passed, so the exact value only matters for animations.
_FRAME_SEC = 1.0 / 60.0

# Synthetic FM TX stream: a fixed 3:48 file mid-playout.  The elapsed time
# is deliberately behind the sent position (the client leads realtime), so
# ticking the view never pushes a chunk — snapshots stay deterministic.
_TX_FILE = 'pirate_radio.mp3'
_TX_DIR = '/home/tac/audio'
_TX_TOTAL_S = 228.0
_TX_RATE = 44100
_TX_SENT_S = 96.4
_TX_ELAPSED_S = 94.4

# Fixed wall-clock epoch for chrome that renders the time of day.
_SNAPSHOT_EPOCH = 1_700_000_000.0

# View states the harness can synthesize, beyond each view's default feed.
_STATES: dict[str, tuple[str, ...]] = {
    'fm:tx': ('streaming',),
    'status:splash': ('offline', 'styx'),
    'wifi:scan': ('tx',),
    'wifi:traffic': ('tx',),
}

# Synthetic PSD geometry.  The bins are a drifting carrier over a noise
# floor so the waterfall shows movement between rows.
_PSD_BINS = 256
_PSD_FLOOR_DB = -96.0
_PSD_CEIL_DB = -34.0

_CURSOR_RE = re.compile(r'\x1b\[(\d+);(\d+)H')

# SGR attribute codes that ``Text.style`` can emit.
_SGR_ATTRS = {
    1: 'bold', 2: 'faint', 3: 'italic', 4: 'underline',
    5: 'blink', 7: 'negative', 9: 'crossed',
}

VIEWS = ('fm:rx', 'fm:tx', 'status:splash', 'viz:waterfall', 'viz:const',
         'wifi:scan', 'wifi:traffic')


@dataclass(frozen=True)
class CellStyle:
    """Styling state active when a character was written to the grid."""

    fg: tuple[int, int, int] | None = None
    bg: tuple[int, int, int] | None = None
    attrs: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Snapshot:
    """A rendered view: escapes for eyes, styled cell grid for tests.

    The cell grid records the character, foreground, background, and
    attributes active at every screen position.  Aggregate counts are
    derived from the grid so they reflect visible cells, not escape
    sequence occurrences.
    """

    view: str
    theme: str
    width: int
    height: int
    ansi: str
    plain: str
    cells: list[list[tuple[str, CellStyle]]] = field(default_factory=list)
    foregrounds: dict = field(default_factory=dict)
    backgrounds: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)

    def cell_style(self, row: int, col: int) -> CellStyle:
        """Return the styling active at *row*, *col*."""
        return self.cells[row][col][1]

    def cells_with_fg(self, fg: tuple[int, int, int]) -> int:
        """Count how many cells were rendered with foreground *fg*."""
        return self.foregrounds.get(fg, 0)

    def cells_with_bg(self, bg: tuple[int, int, int]) -> int:
        """Count how many cells were rendered with background *bg*."""
        return self.backgrounds.get(bg, 0)

    def summary(self) -> str:
        """One-glance colour and attribute report for human review."""
        def fmt(counts):
            return ', '.join(f'{_name(k)}x{v}'
                             for k, v in sorted(counts.items(), key=repr))
        return (
            f'{self.view} {self.width}x{self.height} {self.theme}\n'
            f'  foregrounds: {fmt(self.foregrounds) or "none"}\n'
            f'  backgrounds: {fmt(self.backgrounds) or "none"}\n'
            f'  attributes:  {fmt(self.attributes) or "none"}'
        )


def _name(key) -> str:
    """Render a colour triple compactly, leaving attribute names alone."""
    if isinstance(key, tuple):
        return '(' + ','.join(str(c) for c in key) + ')'
    return str(key)


def _synthetic_audio() -> list[float]:
    """A deterministic tone with a slow amplitude envelope.

    Determinism is verified per view, not guaranteed by the harness.
    """
    return [math.sin(i / 9.0) * (0.3 + 0.5 * math.sin(i / 400.0))
            for i in range(_AUDIO_N)]


def _synthetic_rds() -> RdsState:
    """A fully-populated, deterministic RDS state for the FM RX view."""
    rt = 'Hack The Planet'
    return RdsState(
        synced=True, pi_valid=True, pi=0x8203, pty=10,
        tp=True, ta=False, ms=True, di=0,
        ps='SHESHA  ', ps_mask=0xFF,
        rt=rt.ljust(64), rt_len=len(rt), rt_mask=(1 << len(rt)) - 1,
        rt_ab=False, rt_valid=True,
        ptyn='TEST', ptyn_mask=0xFF, ptyn_ab=False,
        af=[126, 92], ct_mjd=61000, ct_minutes=12 * 60 + 34,
        ct_offset_half_hours=-10, ct_valid=True,
        oda=[], groups=0, blocks=0, block_errors=0, sync_losses=0,
    )


def _synthetic_bins(frame: int) -> list[float]:
    """A drifting carrier over a noise floor, one frame of movement."""
    center = _PSD_BINS / 2 + 12.0 * math.sin(frame / 3.0)
    return [
        _PSD_FLOOR_DB
        + 62.0 * math.exp(-((k - center) ** 2) / (2 * 24 ** 2))
        + 2.5 * math.sin(k / 5.0 + frame / 4.0)
        for k in range(_PSD_BINS)
    ]


def _psd_frame(frame: int) -> dict:
    """A daemon-shaped PSD frame."""
    return {
        'type': 'psd',
        # 50 fps worth of daemon-timestamp spacing, matching the feed loop.
        'ts': float(frame) * 0.02,
        'bins': _synthetic_bins(frame),
        'db_floor': _PSD_FLOOR_DB,
        'db_ceil': _PSD_CEIL_DB,
        'center_freq_mhz': 5180.0,
    }


# 16-QAM symbol grid, deterministic noise, so snapshots are byte-identical.
_QAM16 = [(i & 3, i >> 2) for i in range(16)]


def _synthetic_iq_frame(frame: int) -> dict:
    from ..protocol import pack_iq
    pts = []
    for k in range(1024):
        a, b = _QAM16[(k + frame) % 16]
        noise = 0.04 * math.sin(k / 7.0 + frame / 5.0)
        pts.append(complex((a - 1.5) / 1.5 + noise,
                           (b - 1.5) / 1.5 + noise))
    return {
        'type': 'iq', 'ts': float(frame) * 0.02, 'bits': 8,
        'n_samples': len(pts), 'samples': pack_iq(pts, 8),
        'sample_rate_hz': 2_100_000, 'freq_mhz': 915.0,
    }


# App overview shown on the status splash, pulled from the live registry so
# the wording lives with each app (``App.description``).
_SYNTHETIC_APPS = [(cls.name, cls.description) for cls in APP_CLASSES]


def _stepping_clock(start: float = 1_700_000_000.0,
                    step: float = 1.0):
    """A monotonic fake clock that advances on every call.

    Injected into time-gated views so a snapshot run does not depend on
    how fast the machine renders.
    """
    ticks = itertools.count(start, step)
    return lambda: next(ticks)


def _fixed_clock() -> float:
    """A monotonic clock frozen at the snapshot epoch.

    Injected into time-gated views (FM TX progress) so a render does not
    depend on when it ran.
    """
    return _SNAPSHOT_EPOCH


class _FakeAudio:
    """Stand-in for decoded samples without materialising the whole track."""

    def __init__(self, n: int) -> None:
        self._n = n

    def __len__(self) -> int:
        return self._n

    def __bool__(self) -> bool:
        return True

    def __getitem__(self, item):
        if isinstance(item, slice):
            start, stop, step = item.indices(self._n)
            count = len(range(start, stop, step))
            base = _synthetic_audio()
            return [base[i % len(base)] for i in range(count)]
        return _synthetic_audio()[item % _AUDIO_N]


class _SyntheticDaemon:
    """A daemon with fixed state and TX telemetry for shell snapshots.

    The shell chrome reads daemon state (``tx_busy``, ``freq``, ``gain``)
    and the FM TX view reads ``tx_stats()``; both must be constant for
    byte-identical renders.  Nothing here talks to hardware.
    """

    def __init__(self, state: str | None = None) -> None:
        streaming = state == 'streaming'
        wifi_tx = state == 'tx'
        self.connected = True
        self.busy = False
        self.connection_state = 'connected'
        self._host_display = '192.168.2.1:5555'
        self._state = {
            'freq': '2437000000' if wifi_tx else '100100000',
            'bw': '20000000' if wifi_tx else '200000',
            'sample_rate': '20000000',
            'gain': '50',
            'agc_mode': 'manual', 'mode': '3', 'fpga_active': '0',
            'tx_busy': '1' if (streaming or wifi_tx) else '0',
            'tx_proto': str(TX_PROTO_FM if streaming else
                            TX_PROTO_WIFI if wifi_tx else TX_PROTO_NONE),
            'tx_repeat': '10' if wifi_tx else '0',
            'tx_flags': '0',
            'tx_interval_us': '5000' if wifi_tx else '0',
            'tx_atten': '-10.0' if wifi_tx else '0.0',
        }
        dac_rate = 2_100_000
        emitted = int(_TX_ELAPSED_S * dac_rate * 0.9987)
        self._stats = TxStats(
            tx_active=streaming, fm_active=True, end_of_stream=False,
            dac_rate=dac_rate, fed=emitted + 445_000, emitted=emitted,
            elapsed_us=int(_TX_ELAPSED_S * 1_000_000),
            depth=int(0.084 * dac_rate), max_depth=int(0.11 * dac_rate),
            high_water=int(0.13 * dac_rate), underruns=0,
            ring_fill=int(0.620 * _TX_RATE), ring_size=262144,
            audio_rate=_TX_RATE, audio_consumed=emitted,
            starved_samples=0, push_rejects=0,
            iq_active=False, iq_ring_fill=0, iq_push_rejects=0,
            iq_starved=0)

    def set_ui_thread(self, tid) -> None:
        """Shell pins callbacks to the UI thread; snapshots are single-threaded."""

    def on_change(self, fn) -> None:
        """Shell registers a change callback; snapshots never fire it."""

    def get_state(self) -> dict:
        return dict(self._state)

    @property
    def host_display(self) -> str:
        return self._host_display

    def set_mode(self, mode: int) -> None:
        self._state['mode'] = str(mode)

    def set_iq_bits(self, bits: int) -> None:
        self._state['iq_bits'] = str(bits)

    def tx_stats(self) -> TxStats:
        return self._stats


def _wifi_ie(**kwargs) -> SimpleNamespace:
    """An IE summary object with the daemon's attribute names."""
    fields = dict(channel=0, security='unknown', cipher='unknown',
                  phy_flags=0, phy='', max_rate=0.0, n_ss=0,
                  chan_width=0, country='', pmf='none')
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _wifi_frame(ts: float, fclass: str, *, bssid: str = '', ssid: str = '',
                sa: str = '', da: str = '', is_eapol: bool = False,
                ie: SimpleNamespace | None = None, gain: float = 50.0,
                rx_freq_hz: int = 5_180_000_000,
                fcs_ok: bool = True) -> dict:
    """A daemon-shaped wifi frame."""
    return {
        'type': 'wifi',
        'ts': ts,
        'rate_code': 0,
        'rate_mbps': 24,
        'fcs_ok': fcs_ok,
        'length': 300,
        'frame_class': 3,
        'frame_class_name': fclass,
        'rx_gain': gain,
        'rx_freq_hz': rx_freq_hz,
        'psdu': b'',
        'meta': SimpleNamespace(bssid=bssid, sa=sa, da=da, ssid=ssid,
                                is_eapol=is_eapol),
        'ie': ie,
    }


def _synthetic_wifi_frames() -> list[dict]:
    """One scan's worth of frames: a grouped AP, a client, a wanderer."""
    return [
        _wifi_frame(100.0, 'beacon', bssid='aa:bb:cc:00:00:01', ssid='Home',
                    ie=_wifi_ie(channel=36, security='wpa2-psk',
                                cipher='ccmp', phy='VHT', max_rate=866.0,
                                n_ss=2, chan_width=2, country='CA'),
                    gain=55.0),
        _wifi_frame(100.0, 'beacon', bssid='aa:bb:cc:00:00:02', ssid='Home',
                    ie=_wifi_ie(channel=36, security='wpa2-psk',
                                cipher='ccmp', phy='VHT', max_rate=866.0,
                                n_ss=2, chan_width=2),
                    gain=48.0),
        _wifi_frame(107.0, 'beacon', bssid='10:20:30:40:50:60',
                    ssid='CafeNet',
                    ie=_wifi_ie(channel=6, security='open', phy='HT',
                                max_rate=144.0, n_ss=1, chan_width=0),
                    gain=31.0),
        _wifi_frame(107.5, 'beacon', bssid='10:20:30:40:50:61', ssid='',
                    ie=_wifi_ie(channel=6, security='open'), gain=22.0),
        _wifi_frame(106.0, 'beacon', bssid='20:30:40:50:60:70',
                    ssid='东京都无线局域网',
                    ie=_wifi_ie(channel=11, security='wpa2-psk',
                                cipher='ccmp', max_rate=300.0, n_ss=2),
                    gain=18.0),
        _wifi_frame(110.0, 'data', bssid='aa:bb:cc:00:00:01',
                    sa='de:ad:be:ef:00:01', da='aa:bb:cc:00:00:01',
                    gain=40.0),
        _wifi_frame(111.0, 'data', bssid='aa:bb:cc:00:00:01',
                    sa='de:ad:be:ef:00:02', da='aa:bb:cc:00:00:01',
                    gain=36.0),
        _wifi_frame(112.0, 'eapol', bssid='aa:bb:cc:00:00:01',
                    sa='de:ad:be:ef:00:01', da='aa:bb:cc:00:00:01',
                    is_eapol=True, gain=42.0),
        _wifi_frame(113.0, 'probe_req', sa='de:ad:be:ef:00:03',
                    ssid='RoamNet', gain=28.0),
    ]


def _synthetic_traffic_buckets() -> list[list[dict]]:
    """A rolling window with bursty data and one bad-FCS frame.

    The traffic view counts one bucket per simulated second, so the
    frames are grouped by bucket rather than fed as a flat stream.
    """
    buckets = []
    for bucket in range(8):
        ts = 200.0 + bucket
        frames = []
        for _ in range(2 + bucket % 4):
            frames.append(_wifi_frame(
                ts, 'data', bssid='aa:bb:cc:00:00:01',
                sa='de:ad:be:ef:00:01', da='aa:bb:cc:00:00:01', gain=40.0))
        frames.append(_wifi_frame(ts, 'beacon', bssid='aa:bb:cc:00:00:01',
                                  ssid='Home', ie=_wifi_ie(channel=36),
                                  gain=50.0))
        if bucket % 2 == 0:
            frames.append(_wifi_frame(ts, 'probe_req',
                                      sa='de:ad:be:ef:00:03',
                                      ssid='RoamNet', gain=25.0))
        if bucket == 2:
            frames.append(_wifi_frame(ts, 'deauth', bssid='aa:bb:cc:00:00:01',
                                      sa='de:ad:be:ef:00:01',
                                      da='ff:ff:ff:ff:ff:ff', gain=35.0))
        if bucket == 5:
            frames.append(_wifi_frame(ts, 'eapol', bssid='aa:bb:cc:00:00:01',
                                      sa='de:ad:be:ef:00:02',
                                      da='aa:bb:cc:00:00:01',
                                      is_eapol=True, gain=38.0, fcs_ok=False))
        if bucket == 6:
            frames.append(_wifi_frame(ts, 'disassoc', bssid='aa:bb:cc:00:00:01',
                                      sa='de:ad:be:ef:00:02',
                                      da='aa:bb:cc:00:00:01', gain=34.0))
        buckets.append(frames)
    return buckets


def _build_view(name: str):
    """Return a view instance, without opening any hardware resources."""
    if name == 'fm:rx':
        from .fm import FmRxView
        return FmRxView(player_factory=None)
    if name == 'fm:tx':
        from .fm import FmTxView
        return FmTxView(clock=_fixed_clock)
    if name == 'status:splash':
        from .status import SplashView
        return SplashView()
    if name == 'viz:waterfall':
        from .viz import WaterfallView
        # The fake clock lets every synthetic frame emit a row.
        return WaterfallView(clock=_stepping_clock())
    if name == 'viz:const':
        from .viz import ConstView
        # The fake clock lets every synthetic frame pass the fps gate.
        return ConstView(clock=_stepping_clock())
    if name == 'wifi:scan':
        from .wifi import ScanView
        return ScanView()
    if name == 'wifi:traffic':
        from .wifi import TrafficView
        return TrafficView()
    raise ValueError(f'unknown view {name!r}; try one of {", ".join(VIEWS)}')


def _feed_fm_tx(view, state: str | None) -> None:
    """Seed the transmit view from a fixed, mid-playout stream.

    Only the ``streaming`` preset is synthesized; the idle view needs no
    feed.  Elapsed time trails the sent position by more than the TX lead
    window, so a subsequent ``tick()`` sees no room to push a chunk and
    the state machine stays put.
    """
    if state != 'streaming':
        return
    if view.daemon is None:
        view.daemon = _SyntheticDaemon(state)
    from .fm import Track
    def track(name: str, duration: float) -> 'Track':
        return Track(f'{_TX_DIR}/{name}', duration=duration, loaded=True)

    view._playlist = [
        track(_TX_FILE, _TX_TOTAL_S),
        track('station_id.wav', 12.0),
        track('mixtape_side_a.mp3', 312.0),
        track('test_tone.f32', 1.0),
        track('evening_jazz.mp3', 384.0),
        track('late_night_news.mp3', 243.0),
    ]
    view._selected = 2
    view._playing = 0
    view._file_duration = _TX_TOTAL_S
    view._audio_rate = _TX_RATE
    view._audio = _FakeAudio(int(_TX_TOTAL_S * _TX_RATE))
    view._sent = int(_TX_SENT_S * _TX_RATE)
    view._retries = 0
    view._state = 'streaming'
    view._status_msg = 'streaming...'
    view._stream_t0 = view._clock() - _TX_ELAPSED_S
    # Live RDS fields, so the now-playing panel shows a real broadcast line.
    view._rds_on = True
    view._rds_ps = 'TESTFM  '
    view._rds_rt = 'Pirate Radio request line 555-0199'
    view._rds_pi = 0x8203
    view._rds_pty = 10


def _wifi_tx_info():
    """The TX descriptor the WiFi status bar renders in snapshots."""
    from .wifi import WifiTxInfo

    return WifiTxInfo(
        frame_type='deauth', rate_mbps=6, psdu_len=26,
        da=bytes.fromhex('aabbccddeeff'),
        bssid=bytes.fromhex('112233445566'), reason=7)


def _feed(view, name: str, height: int, state: str | None = None) -> None:
    """Push synthetic data so the view has something to render.

    Every feed is deterministic, so repeated renders are byte-identical.
    The traffic view gets whole buckets and advances its own clock; the
    waterfall gets enough frames to fill its history. The transmit views
    take ``state`` presets: ``streaming`` for FM, ``tx`` for WiFi. Their
    telemetry needs a daemon, which the shell renderer supplies.
    """
    if name.startswith('wifi:') and state == 'tx':
        if view.daemon is None:
            view.daemon = _SyntheticDaemon(state)
        view._tx_bar.info = _wifi_tx_info()

    if name == 'fm:tx':
        _feed_fm_tx(view, state)
    elif name == 'fm:rx':
        view.update({'type': 'fm', 'audio': _synthetic_audio(),
                     'audio_rate': 43750, 'rds': _synthetic_rds()})
        view.tick(_FRAME_SEC)
    elif name == 'status:splash':
        if view.daemon is None:
            view.daemon = _SyntheticDaemon(state)
        view.daemon.connected = state != 'offline'
        features = 0 if state == 'styx' else 0x03 | (1 << 2) | (1 << 3)
        view.set_device(features=features)
        view.set_apps(_SYNTHETIC_APPS)
        for i in range(60):
            view.update(_psd_frame(i))
    elif name == 'viz:waterfall':
        view.set_target_fps(50)
        view.set_coverage(0.997)
        view.set_workers(2)
        # Two pixel rows per character row; fill the pane and then some.
        for i in range(height * 2 + 4):
            view.update(_psd_frame(i))
    elif name == 'viz:const':
        for i in range(12):
            view.update(_synthetic_iq_frame(i))
    elif name == 'wifi:scan':
        for frame in _synthetic_wifi_frames():
            view.update(frame)
    elif name == 'wifi:traffic':
        for bucket in _synthetic_traffic_buckets():
            for frame in bucket:
                view.update(frame)
            view.tick(1.0)


def _parse_sgr(params: str, fg, bg, attrs):
    """Interpret the semicolon-separated parameter list of an SGR sequence.

    Returns updated (fg, bg, attrs).  Only the seven attribute codes that
    ``Text.style`` emits are recognised.
    """
    codes = [int(c) for c in params.split(';') if c != '']
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            fg = None
            bg = None
            attrs = frozenset()
        elif c == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
            fg = (codes[i + 2], codes[i + 3], codes[i + 4])
            i += 4
        elif c == 48 and i + 4 < len(codes) and codes[i + 1] == 2:
            bg = (codes[i + 2], codes[i + 3], codes[i + 4])
            i += 4
        elif c in _SGR_ATTRS:
            attrs = attrs | {_SGR_ATTRS[c]}
        i += 1
    return fg, bg, attrs


def _parse_grid(raw: str, width: int, height: int
                ) -> tuple[list[list[tuple[str, CellStyle]]], str]:
    """Walk the ANSI stream cell by cell, recording character and style.

    Returns a styled cell grid and the plain-text equivalent.  Raises on
    unrecognised escape sequences or writes outside the grid so that new
    terminal output patterns are caught instead of silently misinterpreted.

    ``\\x9b`` (CSI shorthand) is not emitted by pytui and is rejected.
    ``\\r`` and ``\\n`` are not emitted by ``Window.draw`` and are rejected.
    """
    default_style = CellStyle()
    grid = [
        [(' ', default_style)] * width
        for _ in range(height)
    ]
    fg = None
    bg = None
    attrs: frozenset[str] = frozenset()
    x = y = 0
    i = 0

    while i < len(raw):
        ch = raw[i]

        if ch == '\x9b':
            raise ValueError(
                f'CSI shorthand \\x9b at position {i}; '
                'pytui only emits \\x1b[')

        if ch in ('\r', '\n'):
            raise ValueError(
                f'unexpected {ch!r} at position {i}; '
                'Window.draw does not emit newlines or carriage returns')

        if ch == '\x1b':
            # Cursor move: \x1b[row;colH
            move = _CURSOR_RE.match(raw, i)
            if move:
                y = int(move.group(1)) - 1
                x = int(move.group(2)) - 1
                i = move.end()
                continue

            # Any other recognised ANSI sequence.
            escape = ANSI_SEQUENCE.match(raw, i)
            if not escape:
                raise ValueError(
                    f'unrecognised escape sequence at position {i}: '
                    f'{raw[i:i+20]!r}')

            seq = escape.group()
            # SGR sequences end with 'm'.
            if seq.endswith('m'):
                params = seq[2:-1]  # strip \x1b[ and m
                fg, bg, attrs = _parse_sgr(params, fg, bg, attrs)
            i = escape.end()
            continue

        # Printable character — must land inside the grid.  A wide
        # character covers its own cell and the one to its right.
        columns = char_width(ch)
        if columns == 0 and x > 0 and 0 <= y < height:
            # Combining mark: fold it into the cell it modifies.
            prev_ch, prev_style = grid[y][x - 1]
            grid[y][x - 1] = (prev_ch + ch, prev_style)
            i += 1
            continue
        if (y < 0 or y >= height or x < 0
                or x + max(1, columns) > width):
            raise ValueError(
                f'write at ({y}, {x}) outside {height}x{width} grid: '
                f'{ch!r} at position {i}')

        style = CellStyle(fg=fg, bg=bg, attrs=attrs)
        grid[y][x] = (ch, style)
        if columns == 2 and x + 1 < width:
            grid[y][x + 1] = ('', style)
        x += max(1, columns)
        i += 1

    plain = '\n'.join(''.join(cell[0] for cell in row) for row in grid)
    return grid, plain


def _count_cells(grid):
    """Derive aggregate colour and attribute counts from the cell grid."""
    fg_counts: dict[tuple[int, int, int], int] = collections.Counter()
    bg_counts: dict[tuple[int, int, int], int] = collections.Counter()
    attr_counts: dict[str, int] = collections.Counter()
    for row in grid:
        for _ch, style in row:
            if style.fg is not None:
                fg_counts[style.fg] += 1
            if style.bg is not None:
                bg_counts[style.bg] += 1
            for a in style.attrs:
                attr_counts[a] += 1
    return fg_counts, bg_counts, attr_counts


def _validate_state(name: str, state: str | None) -> None:
    """Reject a preset the harness cannot synthesize for *name*."""
    if state is None:
        return
    allowed = _STATES.get(name, ())
    if state not in allowed:
        raise ValueError(
            f'unknown state {state!r} for {name!r}; '
            f'valid: {", ".join(allowed) if allowed else "none"}')


def _build_shell(name: str, width: int, height: int, theme: str,
                 state: str | None) -> list:
    """Build the full terminal chrome around a view.

    Reuses the real ``Shell`` layout so status bar, TX bar, body and input
    bar are rendered by the same code the live TUI runs.  The target view
    is swapped for the harness's synthetic-feed instance, and a config,
    daemon and clock are supplied so the frame is deterministic.
    """
    from .config import Config
    from .shell import Shell

    app_key, view_name = name.split(':', 1)
    apps = [cls() for cls in APP_CLASSES]
    target = {a.name: a for a in apps}[app_key]
    target.views[view_name] = _build_view(name)

    fd, path = tempfile.mkstemp(suffix='.json', prefix='shesha-snapshot-')
    os.close(fd)
    try:
        config = Config(path)
        config.set('theme', theme)
        daemon = _SyntheticDaemon(state)
        shell = Shell(apps=apps, config=config, daemon=daemon,
                      clock=lambda: time.gmtime(_SNAPSHOT_EPOCH))
        # The view swapped in above predates the shell's daemon wiring.
        target.views[view_name].config = config
        target.views[view_name].daemon = daemon
        target.views[view_name].theme_changed(shell.style)
        shell._active_app = app_key
        target.switch_view(view_name)
        shell._apply_view_mode(target.active_view)

        view = target.active_view
        root = StyledWindow(0, 0, width, height,
                            {'bg': shell.style['bg'], 'fg': shell.style['fg']})
        windows = shell._build_layout(root)
        # Views that render into their panes (waterfall, constellation) drop
        # frames until layout has created the windows, so feed after it.
        _feed(view, name, height, state=state)
        # Layout builds the panes; tick fills the dynamic ones (scope,
        # progress) now that their windows exist.
        view.tick(_FRAME_SEC)
        return windows
    finally:
        os.unlink(path)


def render_view(name: str, width: int = 120, height: int = 30,
                theme: str = DEFAULT_STYLE, *, shell: bool = False,
                state: str | None = None) -> Snapshot:
    """Render a view off-screen and return its styled cell grid.

    Args:
        name:   View identifier, e.g. ``'fm:rx'``.
        width:  Terminal columns.
        height: Rows. Without *shell* this is the view; with it, the whole
            terminal (status bar and input bar included).
        theme:  Palette name from ``STYLES``.
        shell:  Draw the full terminal chrome around the view.
        state:  Optional view preset; ``'streaming'`` for ``'fm:tx'``.
    """
    if theme not in STYLES:
        raise ValueError(f'unknown theme {theme!r}; '
                         f'try one of {", ".join(sorted(STYLES))}')
    _validate_state(name, state)
    style = STYLES[theme]

    if shell:
        windows = _build_shell(name, width, height, theme, state)
    else:
        view = _build_view(name)
        daemon = _SyntheticDaemon(state)
        view.daemon = daemon
        for key in daemon.get_state():
            view.config_changed(key, daemon)
        view.theme_changed(style)
        root = StyledWindow(0, 0, width, height,
                            {'bg': style['bg'], 'fg': style['fg']})
        windows = view.layout(root)
        _feed(view, name, height, state=state)
        view.tick(_FRAME_SEC)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for window in windows:
            window.draw()
    # Park cursor below the render so a shell prompt does not overwrite it.
    raw = buf.getvalue() + f'\x1b[{height + 1};1H'

    grid, plain = _parse_grid(raw, width, height)
    fg_counts, bg_counts, attr_counts = _count_cells(grid)

    return Snapshot(
        view=name,
        theme=theme,
        width=width,
        height=height,
        ansi=raw,
        plain=plain,
        cells=grid,
        foregrounds=fg_counts,
        backgrounds=bg_counts,
        attributes=attr_counts,
    )


def _positive_int(value: str) -> int:
    """Argparse type that rejects zero and negative values."""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f'{value} is not a positive integer')
    return n


def main() -> None:
    p = argparse.ArgumentParser(
        description='Render a TUI view headlessly',
        epilog='With no options the escape stream goes to stdout, so '
               'piping it to a truecolor terminal shows the view.')
    p.add_argument('view', choices=VIEWS, metavar='VIEW',
                   help=f'view to render: {", ".join(VIEWS)}')
    p.add_argument('--width', type=_positive_int, default=120,
                   help='terminal columns for the view (default 120)')
    p.add_argument('--height', type=_positive_int, default=30,
                   help='rows (view rows, or terminal rows with --shell; '
                        'default 30)')
    p.add_argument('--theme', default=DEFAULT_STYLE, choices=sorted(STYLES),
                   help=f'palette to use (default {DEFAULT_STYLE})')
    p.add_argument('--shell', action='store_true',
                   help='draw the full terminal chrome (status/TX/input bars)')
    p.add_argument('--state', default=None,
                   help='view state preset; fm:tx accepts streaming')
    p.add_argument('-o', '--out', help='write the escape stream here too')
    p.add_argument('--plain', action='store_true',
                   help='print the plain character grid')
    p.add_argument('--summary', action='store_true',
                   help='print colour and attribute counts')
    args = p.parse_args()

    try:
        snap = render_view(args.view, args.width, args.height, args.theme,
                           shell=args.shell, state=args.state)
    except ValueError as exc:
        sys.exit(str(exc))

    if args.plain:
        print(snap.plain)
    if args.summary:
        print(snap.summary(), file=sys.stderr)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(snap.ansi)
        size = len(snap.ansi.encode('utf-8'))
        print(f'wrote {args.out} ({size} bytes) — '
              f'view it with: cat {args.out}', file=sys.stderr)
    elif not args.plain and not args.summary:
        sys.stdout.write(snap.ansi)


if __name__ == '__main__':
    main()
