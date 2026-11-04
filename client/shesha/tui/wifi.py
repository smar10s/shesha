"""WiFi app: AP scan display with detail panel.

Shows a unified scrollable table of access points and stations with a
right-side detail panel for the selected row. Uses the shared ScanState
model for AP/STA tracking, grouping, and age-out.

Also provides TX operations (beacon and frame) as app-level commands.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable, TYPE_CHECKING

from .app import App, View
from .command_model import Command
from .completion import complete_path
from .config import KeySpec
from .pytui import (
    StyledWindow, Window, Keyboard, Plot, Text, display_width, sanitize_text,
)
from .table import Table
from .theme import DEFAULT_PALETTE
from ..channels import (
    CHANNEL_TO_FREQ, freq_to_channel as _freq_mhz_to_channel,
)
from ..frames import (
    BROADCAST, DEFAULT_BSSID, build_beacon, build_deauth, build_raw,
    format_mac, parse_mac,
)
from ..scan import ScanState
from ..oui import manufacturer as _oui_lookup
from ..protocol import DaemonMode, TX_PROTO_WIFI, TX_REPEAT_FOREVER, TxFlags

# Sorted master channel list for scan iteration
_ALL_CHANNELS: list[int] = sorted(CHANNEL_TO_FREQ.keys())

_WINDOW_SIZES = [10, 30, 60, 120, 300]
_DEFAULT_WINDOW = 60

if TYPE_CHECKING:
    from .daemon import Daemon


class _DictFrame:
    """Adapt a daemon wifi frame dict to the attribute interface ScanState expects."""
    __slots__ = ('timestamp', 'rate_code', 'rate_mbps', 'fcs_ok', 'length',
                 'frame_class', 'frame_class_name', 'rx_gain', 'rx_freq_hz',
                 'psdu', 'meta', 'ie')

    def __init__(self, d: dict):
        self.timestamp = d.get('ts', 0.0)
        self.rate_code = d.get('rate_code', 0)
        self.rate_mbps = d.get('rate_mbps', 0)
        self.fcs_ok = d.get('fcs_ok', False)
        self.length = d.get('length', 0)
        self.frame_class = d.get('frame_class', 9)
        self.frame_class_name = d.get('frame_class_name', 'unknown')
        self.rx_gain = d.get('rx_gain', 0.0)
        self.rx_freq_hz = d.get('rx_freq_hz', 0)
        self.psdu = d.get('psdu', b'')
        self.meta = d.get('meta')
        self.ie = d.get('ie')


class _TrafficBucket:
    """A 1-second bucket of traffic counts."""
    __slots__ = ('ts', 'counts', 'fcs_ok_by_type', 'fcs_bad_by_type')

    def __init__(self, ts: float):
        self.ts = ts
        self.counts: dict[str, int] = {}
        self.fcs_ok_by_type: dict[str, int] = {}
        self.fcs_bad_by_type: dict[str, int] = {}


class TrafficState:
    """Rolling window of 1-second traffic buckets — per-type counts + FCS."""

    def __init__(self, window_sec: int = 60):
        self._buckets: deque[_TrafficBucket] = deque(maxlen=window_sec)
        self._current = _TrafficBucket(0.0)

    def update(self, frame: dict) -> None:
        """Increment counts for the current bucket from a wifi frame dict."""
        name = frame.get('frame_class_name', 'unknown')
        self._current.counts[name] = self._current.counts.get(name, 0) + 1
        if frame.get('fcs_ok'):
            self._current.fcs_ok_by_type[name] = (
                self._current.fcs_ok_by_type.get(name, 0) + 1)
        else:
            self._current.fcs_bad_by_type[name] = (
                self._current.fcs_bad_by_type.get(name, 0) + 1)

    def advance_bucket(self, t: float) -> None:
        """Push the current bucket into the window and start a new one."""
        self._buckets.append(self._current)
        self._current = _TrafficBucket(t)

    @property
    def buckets(self) -> list[_TrafficBucket]:
        return list(self._buckets)

    def totals(self) -> dict[str, dict]:
        """Return {type_name: {count, rate, pct, fcs_ok_pct}} plus 'ALL'."""
        agg: dict[str, int] = {}
        fcs_ok: dict[str, int] = {}
        fcs_bad: dict[str, int] = {}
        for b in self._buckets:
            for name, count in b.counts.items():
                agg[name] = agg.get(name, 0) + count
            for name, c in b.fcs_ok_by_type.items():
                fcs_ok[name] = fcs_ok.get(name, 0) + c
            for name, c in b.fcs_bad_by_type.items():
                fcs_bad[name] = fcs_bad.get(name, 0) + c

        total_frames = sum(agg.values())
        total_fcs_ok = sum(fcs_ok.values())
        total_fcs_bad = sum(fcs_bad.values())
        n_buckets = max(1, len(self._buckets))

        result: dict[str, dict] = {}
        for name in sorted(agg, key=agg.get, reverse=True):
            count = agg[name]
            rate = count / n_buckets
            pct = (count / total_frames * 100) if total_frames > 0 else 0.0
            fcs_total = fcs_ok.get(name, 0) + fcs_bad.get(name, 0)
            fcs_ok_pct = (fcs_ok.get(name, 0) / fcs_total * 100) if fcs_total > 0 else 0.0
            result[name] = {
                'count': count, 'rate': rate, 'pct': pct,
                'fcs_ok_pct': fcs_ok_pct,
            }

        total_fcs = total_fcs_ok + total_fcs_bad
        result['ALL'] = {
            'count': total_frames,
            'rate': total_frames / n_buckets,
            'pct': 100.0,
            'fcs_ok_pct': (total_fcs_ok / total_fcs * 100) if total_fcs > 0 else 0.0,
        }
        return result

    def values_for_type(self, type_name: str) -> list[int]:
        """Return per-bucket count values for a given type (or ALL)."""
        if type_name == 'ALL':
            return [sum(b.counts.values()) for b in self._buckets]
        return [b.counts.get(type_name, 0) for b in self._buckets]

    def resize(self, window_sec: int) -> None:
        """Change the rolling window size (truncates if smaller)."""
        if window_sec == self._buckets.maxlen:
            return
        self._buckets = deque(self._buckets, maxlen=window_sec)

    def reset(self) -> None:
        """Clear all history."""
        maxlen = self._buckets.maxlen
        self._buckets = deque(maxlen=maxlen)
        self._current = _TrafficBucket(0.0)


@dataclass(frozen=True)
class WifiTxInfo:
    """What the TUI last asked the daemon to transmit.

    The daemon reports the schedule but not the frame type or addresses,
    so the view keeps these for its status bar.
    """

    frame_type: str
    rate_mbps: int
    psdu_len: int
    ssid: str | None = None
    da: bytes | None = None
    bssid: bytes | None = None
    reason: int | None = None


def _mac_forms(addr: bytes) -> list[str]:
    """MAC renderings from most to least informative."""
    full = format_mac(addr)
    parts = full.split(':')
    return [
        full,
        f'{parts[0]}:{parts[1]}:…:{parts[4]}:{parts[5]}',
        f'{parts[0]}:…:{parts[5]}',
        f'…:{parts[5]}',
    ]


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ''
    if display_width(text) <= width:
        return text
    if width == 1:
        return '…'
    out = ''
    for ch in text:
        if display_width(out + ch) + 1 > width:
            break
        out += ch
    return out + '…'


def _ssid_text(ssid: str, budget: int) -> str:
    if budget < 2:
        return ''
    ssid = sanitize_text(ssid)
    quoted = f'"{ssid}"'
    if display_width(quoted) <= budget:
        return quoted
    return '"' + _truncate(ssid, budget - 2) + '"'


def _wifi_tx_active(state: dict) -> bool:
    if state.get('tx_busy', '0') != '1':
        return False
    try:
        return int(state.get('tx_proto', '-1')) == TX_PROTO_WIFI
    except (TypeError, ValueError):
        return False


def _schedule_text(state: dict) -> str:
    try:
        flags = int(state.get('tx_flags', '0'))
    except (TypeError, ValueError):
        flags = 0
    if flags & TxFlags.CYCLIC:
        return 'cyclic'
    try:
        repeat = int(state.get('tx_repeat', '1'))
    except (TypeError, ValueError):
        repeat = 1
    try:
        interval = int(state.get('tx_interval_us', '0'))
    except (TypeError, ValueError):
        interval = 0
    if repeat == TX_REPEAT_FOREVER:
        count = '∞'
    elif repeat <= 1:
        count = '1x'
    else:
        count = f'{repeat}x'
    if interval <= 0:
        return f'{count} blast'
    if interval % 1000 == 0:
        return f'{count}@{interval // 1000}ms'
    return f'{count}@{interval}us'


def _channel_text(state: dict) -> str:
    try:
        freq_hz = int(float(state.get('freq', '0')))
    except (TypeError, ValueError):
        freq_hz = 0
    ch = _freq_mhz_to_channel(freq_hz / 1e6) if freq_hz else 0
    return f'ch{ch}' if ch else '--'


def _atten_text(state: dict) -> str:
    try:
        atten = float(state.get('tx_atten', '0'))
    except (TypeError, ValueError):
        atten = 0.0
    return f'{atten:g}dB'


def _beacon_target(info: WifiTxInfo, budget: int) -> str:
    label = 'ssid '
    ssid = info.ssid or ''
    if budget <= display_width(label):
        return _truncate(ssid, budget)
    text = label + _ssid_text(ssid, budget - display_width(label))
    used = display_width(text)
    if info.bssid is not None:
        for form in _mac_forms(info.bssid):
            piece = f'  bssid {form}'
            if used + display_width(piece) <= budget:
                text += piece
                used += display_width(piece)
                break
    piece = f'  {info.psdu_len}B'
    if used + display_width(piece) <= budget:
        text += piece
    return text


def _deauth_target(info: WifiTxInfo, budget: int) -> str:
    da_forms = _mac_forms(info.da) if info.da else ['--']
    bssid_forms = _mac_forms(info.bssid) if info.bssid else ['--']
    reason = f' r{info.reason}' if info.reason is not None else ''

    for total in range(len(da_forms) + len(bssid_forms) - 1):
        for i in range(len(da_forms)):
            j = total - i
            if not 0 <= j < len(bssid_forms):
                continue
            base = f'da {da_forms[i]} > {bssid_forms[j]}'
            for suffix in (reason, ''):
                text = base + suffix
                if display_width(text) <= budget:
                    return text

    for form in da_forms:
        for suffix in (reason, ''):
            text = f'da {form}{suffix}'
            if display_width(text) <= budget:
                return text
    for form in da_forms:
        if display_width(form) <= budget:
            return form
    return _truncate('da', budget)


def _target_text(info: WifiTxInfo | None, budget: int) -> str:
    if budget <= 0:
        return ''
    if info is None:
        return '--' if budget >= 2 else '…'
    if info.frame_type == 'beacon':
        return _beacon_target(info, budget)
    if info.frame_type == 'deauth':
        return _deauth_target(info, budget)
    return _truncate(f'{info.psdu_len}B', budget)


def format_wifi_tx_bar(info: WifiTxInfo | None, state: dict, width: int,
                       style: dict) -> str:
    """Render one WiFi TX status row, fitting *width* columns."""
    active = _wifi_tx_active(state)
    bg = style['header_bg']
    tx_color = style['warn'] if active else style['dim']
    value_color = style['header_fg'] if active else style['dim']

    if active:
        type_text = info.frame_type if info is not None else 'wifi'
        rate = f'{info.rate_mbps}M' if info is not None else '--'
        schedule = _schedule_text(state)
        channel = _channel_text(state)
        atten = _atten_text(state)
    else:
        type_text = '--'
        rate = schedule = channel = atten = '--'

    right = [rate, schedule]
    if width >= 52:
        right.append(channel)
    if width >= 80:
        right.append(atten)

    type_field = f'{type_text:<6}'
    prefix = f' tx  {type_field} '
    while right and (display_width(prefix) + display_width('  '.join(right))
                     + 1 > width):
        right.pop(0)

    right_text = '  '.join(right)
    budget = width - display_width(prefix) - display_width(right_text) - 1
    target = _target_text(info if active else None, max(0, budget))
    gap = max(0, width - display_width(prefix) - display_width(target)
              - display_width(right_text))

    def seg(text: str, color) -> str:
        return Text(text).style({'fg': color, 'bg': bg})

    return (seg(' tx ', tx_color)
            + seg(f' {type_field} ', value_color)
            + seg(target, value_color)
            + ' ' * gap
            + seg(right_text, value_color))


class WifiTxBar:
    """Always-visible WiFi TX row shared by the scan and traffic views."""

    def __init__(self) -> None:
        self.info: WifiTxInfo | None = None
        self._was_active = False

    def render(self, win: StyledWindow, style: dict, state: dict) -> None:
        active = _wifi_tx_active(state)
        if self._was_active and not active:
            self.info = None
        self._was_active = active
        win.update_buffer([win.justify_line(
            format_wifi_tx_bar(self.info, state, win.width, style), 'left')])


class ScanView(View):
    """WiFi scan view — unified AP + STA table with detail panel."""

    name = 'scan'
    hints = 'j/k:select  s:scan  g/G:top/bottom  PgUp/PgDn:page'
    mode = DaemonMode.WIFI

    def __init__(self, tx_bar: WifiTxBar | None = None) -> None:
        super().__init__()
        self.style = DEFAULT_PALETTE
        self.table: Table | None = None
        self._detail_win: Window | None = None
        self._bar_win: StyledWindow | None = None
        self._tx_bar = tx_bar or WifiTxBar()
        self._selected = 0
        self._scan = ScanState()
        self._row_index: list[tuple[str, str]] = []  # ('ap', bssid) | ('sta', mac) | ('sep', '')
        self._freq_mhz: float | None = None
        self._daemon = None
        self._drain_needed = False
        self._age_timer = 0.0
        self._dirty = False  # set when new AP/STA discovered; triggers rebuild

        # Channel scanning state
        self._scanning = False
        self._scan_channel_idx = 0  # index into _scan_channels
        self._scan_timer = 0.0      # elapsed time on current channel
        self._scan_channels: list[int] = []  # computed from config on start

    def layout(self, container: StyledWindow) -> list[Window]:
        body, bar = container.hsplit(None, 1)
        # Split body: table (flexible) | detail panel (32 chars, narrower
        # below the usual 80-column floor so the table can still fit).
        detail_w = 32 if body.width >= 80 else max(16, body.width // 3)
        left, right = body.vsplit(None, detail_w)

        self.table = Table(
            left,
            [None, 4, 19, 5, 5],
            header=['SSID', 'Ch', 'BSSID', 'Type', 'Last'],
            header_style={
                'bg': self.style['header_bg'],
                'fg': self.style['header_fg'],
                'bold': True,
            },
            row_style={'bg': self.style['bg'], 'fg': self.style['fg']},
            alt_row_style={
                'bg': self.style['header_bg'], 'fg': self.style['fg']},
            highlight_style={
                'bg': self.style['accent'], 'fg': self.style['bg']},
        )
        self.table.set_column_format(1, {'justify': 'right'})
        self.table.set_column_format(2, {'justify': 'right'})
        self.table.set_column_format(3, {'justify': 'center'})
        self.table.set_column_format(4, {'justify': 'right'})
        for column in (1, 3, 4):
            self.table.set_column_style(
                column, {'fg': self.style['plot_fg']})

        # Detail panel — inherit parent style but could be restyled
        self._detail_win = StyledWindow(
            right.x, right.y, right.width, right.height,
            {'bg': self.style['bg'], 'fg': self.style['fg']})

        self._bar_win = StyledWindow(
            bar.x, bar.y, bar.width, bar.height,
            {'bg': self.style['header_bg'], 'fg': self.style['header_fg']})

        self._update_table()
        self._update_detail()
        self._render_bar()

        windows: list[Window] = [self._detail_win, self._bar_win]
        windows.extend(self.table.columns)
        if self.table.draw_header:
            windows.extend(self.table._header_columns)
        return windows

    # ─── TX Bar ───────────────────────────────────────────────────────

    def _daemon_state(self) -> dict:
        daemon = self._daemon or self.daemon
        return daemon.get_state() if daemon is not None else {}

    def _render_bar(self) -> None:
        if self._bar_win is not None:
            self._tx_bar.render(self._bar_win, self.style,
                                self._daemon_state())

    def theme_changed(self, style: dict) -> None:
        self.style = style
        self._render_bar()

    # ─── Data ─────────────────────────────────────────────────────────

    def config_changed(self, key: str, daemon) -> None:
        """Track frequency and daemon reference from shell."""
        self._daemon = daemon
        if key in ('fpga_active', '_status'):
            fpga = daemon.get_state().get('fpga_active', '0') == '1'
            if fpga and daemon.get_state().get('agc_mode', 'manual') == 'manual':
                daemon.apply('gain', 'auto')
        if key == 'freq':
            state = daemon.get_state()
            freq = state.get('freq')
            if freq is not None:
                try:
                    self._freq_mhz = float(freq) / 1e6
                except (ValueError, TypeError):
                    pass
        self._render_bar()

    def on_enter(self) -> None:
        self._drain_needed = True

    def on_exit(self) -> None:
        self._scanning = False

    def update(self, frame: dict) -> None:
        """Receive a frame from the daemon. Feed ScanState, defer table rebuild."""
        if frame.get('type') != 'wifi' and self._drain_needed:
            return
        self._drain_needed = False

        if frame.get('type') == 'wifi':
            wf = _DictFrame(frame)
            old_ap_count = len(self._scan.aps)
            old_sta_count = len(self._scan.stations)
            self._scan.update(wf)
            # Only flag dirty if topology changed (new AP or STA appeared)
            if len(self._scan.aps) != old_ap_count or len(self._scan.stations) != old_sta_count:
                self._dirty = True

    def tick(self, dt: float) -> None:
        # Channel scan hopping
        if self._scanning and self._scan_channels:
            self._scan_timer += dt
            dwell = self._get_dwell()
            if self._scan_timer >= dwell:
                self._scan_timer = 0.0
                self._scan_channel_idx = (
                    (self._scan_channel_idx + 1) % len(self._scan_channels))
                self._tune_scan_channel()

        self._age_timer += dt
        if self._age_timer > 1.0 or self._dirty:
            self._age_timer = 0.0
            self._dirty = False
            max_age = 120.0
            if self.config:
                max_age = self.config.get_float('wifi.scan.max_age', 120.0)
            self._scan.age_out(max_age_sec=max_age)
            self._rebuild()

        self._render_bar()

    # ─── Channel Scan Control ─────────────────────────────────────────

    def _get_dwell(self) -> float:
        """Get dwell time in seconds from config."""
        if self.config:
            return max(0.1, self.config.get_float('wifi.scan.dwell', 2.0))
        return 2.0

    def _build_scan_channels(self) -> list[int]:
        """Build sorted channel list from start_channel to stop_channel config."""
        start_ch = 1
        stop_ch = 177
        if self.config:
            start_ch = self.config.get_int('wifi.scan.start_channel', 1)
            stop_ch = self.config.get_int('wifi.scan.stop_channel', 177)
        # Filter _ALL_CHANNELS to those in [start_ch, stop_ch] range
        return [ch for ch in _ALL_CHANNELS if start_ch <= ch <= stop_ch]

    def _tune_scan_channel(self) -> None:
        """Tune radio to the current scan channel."""
        if not self._scan_channels:
            return
        ch = self._scan_channels[self._scan_channel_idx]
        freq = CHANNEL_TO_FREQ[ch]
        self.apply_config('freq', str(freq * 1_000_000))

    def _start_scan(self) -> None:
        """Start channel scanning."""
        self._scan_channels = self._build_scan_channels()
        if not self._scan_channels:
            return
        self._scanning = True
        self._scan_channel_idx = 0
        self._scan_timer = 0.0
        self._tune_scan_channel()

    def stop_scan(self) -> None:
        """Stop channel scanning. Stay on current frequency."""
        self._scanning = False

    def command(self, args: list[str]) -> tuple[str | None, str]:
        """Handle :wifi scan start / :wifi scan stop."""
        if not args:
            return (None, '')
        verb = args[0]
        if verb == 'start':
            self._start_scan()
        elif verb == 'stop':
            self.stop_scan()
        else:
            return (f'scan: unknown: {verb}', '')
        return (None, '')

    def word_completions(self) -> list[str]:
        return ['start', 'stop']

    def command_hints(self) -> list[tuple[str, str]]:
        return [('start', 'Start the channel scanner'),
                ('stop', 'Stop the channel scanner')]

    # ─── Table Rendering ──────────────────────────────────────────────

    def _rebuild(self) -> None:
        """Rebuild table rows, preserving selection on the same logical item."""
        if self.table is None:
            return

        # Remember what's selected
        selected_item: tuple[str, str] | None = None
        if self._row_index and 0 <= self._selected < len(self._row_index):
            selected_item = self._row_index[self._selected]

        rows, self._row_index = self._build_rows()

        if not rows:
            rows = self._scanning_row()
            self._row_index = [('sep', '')]

        # Restore selection to same logical item
        if selected_item and selected_item[0] != 'sep':
            for i, item in enumerate(self._row_index):
                if item == selected_item:
                    self._selected = i
                    break
            else:
                # Item disappeared (aged out) — clamp
                self._selected = max(0, min(self._selected, len(self._row_index) - 1))

        # Clamp in case table shrank
        if self._row_index:
            self._selected = min(self._selected, len(self._row_index) - 1)

        # Render with highlight
        self.table.set_highlight(self._selected if rows else None)
        self.table.update(rows)
        self._update_detail()

    # Tree-drawing characters
    _T_BRANCH = '\u251c\u2500'     # ├─ (light branch, for stations)
    _T_CORNER = '\u2514\u2500'     # └─ (light corner, for stations)
    _T_BRANCH_AP = '\u251d\u2501'  # ┝━ (heavy branch, for APs with more below)
    _T_CORNER_AP = '\u2515\u2501'  # ┕━ (heavy corner, for terminal AP)
    _ICON_AP = ' \u2b21 '          #  ⬡  hexagon for APs
    _ICON_STA = ' \u25cf '         #  ●  circle for stations

    def _scanning_row(self) -> list[list[str]]:
        """The empty-table status row, omitting the channel until it is known."""
        if self._freq_mhz is None or self._freq_mhz <= 0:
            return [['Scanning...', '', '', '', '']]
        ch = _freq_mhz_to_channel(self._freq_mhz)
        label = f'ch{ch}' if ch else f'{self._freq_mhz:.1f} MHz'
        return [[f'Scanning {label}...', '', '', '', '']]

    def _build_rows(self) -> tuple[list[list[str]], list[tuple[str, str]]]:
        """Build table rows and parallel row index from ScanState.

        Layout per group:
          ⬡ AP primary         (root, icon only)
          ├─ ● STA-1           (branch, has siblings below)
          ├─ ● STA-2
          ┝━ ⬡ AP secondary    (heavy branch, AP node)
          ├─ ● STA-3
          └─ ● STA-4           (corner, last child)
        """
        rows: list[list[str]] = []
        index: list[tuple[str, str]] = []

        # Use most recent timestamp as "now" for age calculation
        now = max((ap.last_seen for ap in self._scan.aps.values()), default=0.0)
        if not now:
            now = max((s.last_seen for s in self._scan.stations.values()), default=0.0)

        # Group APs by physical device — re-sort by first_seen for stable ordering
        groups = self._scan.group_aps()
        groups.sort(key=lambda g: self._scan.aps[g[0]].first_seen)

        # Collect STAs by BSSID
        stas_by_ap: dict[str, list] = {}
        unassociated: list = []
        for _mac, sta in self._scan.stations.items():
            if sta.bssid in self._scan.aps:
                stas_by_ap.setdefault(sta.bssid, []).append(sta)
            else:
                unassociated.append(sta)

        # Render each group: primary AP as root, then children (secondary APs + STAs)
        for group in groups:
            primary_ap = self._scan.aps[group[0]]
            primary_ssid = primary_ap.ssid or '<hidden>'
            primary_ch = str(primary_ap.channel) if primary_ap.channel else '?'
            primary_age = self._format_age(now - primary_ap.last_seen)
            rows.append([
                self._ssid_cell(self._ICON_AP.lstrip(' ') + primary_ssid,
                                not primary_ap.ssid),
                primary_ch, group[0], 'AP', primary_age])
            index.append(('ap', group[0]))

            # Collect children: secondary APs + stations, in order
            children: list[tuple[str, object]] = []  # ('ap', bssid) | ('sta', StaRecord)

            if len(group) > 1:
                for bssid in group[1:]:
                    children.append(('ap', bssid))

            group_stas = []
            for bssid in group:
                group_stas.extend(stas_by_ap.get(bssid, []))
            group_stas.sort(key=lambda s: s.first_seen)
            for sta in group_stas:
                children.append(('sta', sta))

            # Render children with tree prefixes
            n = len(children)
            for i, (child_type, child_data) in enumerate(children):
                is_last = (i == n - 1)

                if child_type == 'ap':
                    bssid = child_data
                    ap = self._scan.aps[bssid]
                    ap_ssid = ap.ssid or '<hidden>'
                    if ap_ssid == primary_ssid:
                        label = '(same)'
                    else:
                        label = ap_ssid
                    tree = self._T_CORNER_AP if is_last else self._T_BRANCH_AP
                    ssid = tree + self._ICON_AP + label
                    ch = str(ap.channel) if ap.channel else '?'
                    age = self._format_age(now - ap.last_seen)
                    rows.append([self._ssid_cell(ssid, not ap.ssid),
                                 ch, bssid, 'AP', age])
                    index.append(('ap', bssid))
                else:
                    sta = child_data
                    tree = self._T_CORNER if is_last else self._T_BRANCH
                    ssid = tree + self._ICON_STA + sta.mac[-8:]
                    ch = str(sta.channel) if sta.channel else '?'
                    age_s = self._format_age(now - sta.last_seen)
                    rows.append([ssid, ch, sta.mac, 'STA', age_s])
                    index.append(('sta', sta.mac))

        # Unassociated STAs at the end
        if unassociated:
            rows.append(['\u2500\u2500 Unassociated \u2500\u2500', '', '', '', ''])
            index.append(('sep', ''))
            unassociated.sort(key=lambda s: s.first_seen)
            for sta in unassociated:
                ch = str(sta.channel) if sta.channel else '?'
                age_s = self._format_age(now - sta.last_seen)
                rows.append([self._ICON_STA.lstrip(' ') + sta.mac[-8:],
                             ch, sta.mac, 'STA', age_s])
                index.append(('sta', sta.mac))

        return rows, index

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 0:
            return '0s'
        if seconds < 60:
            return f'{seconds:.0f}s'
        if seconds < 3600:
            return f'{seconds / 60:.0f}m'
        return f'{seconds / 3600:.1f}h'

    def _ssid_cell(self, text: str, hidden: bool):
        """Mute an SSID cell whose network hides its name."""
        if hidden:
            return (text, {'fg': self.style['plot_fg']})
        return text

    def _update_table(self) -> None:
        if self.table is None:
            return

        rows, self._row_index = self._build_rows()

        if not rows:
            rows = self._scanning_row()
            self._row_index = [('sep', '')]

        self.table.set_highlight(self._selected if rows else None)
        self.table.update(rows)

    # ─── Detail Panel ─────────────────────────────────────────────────

    def _update_detail(self) -> None:
        if self._detail_win is None:
            return
        self._detail_win.clear()

        if not self._row_index or self._selected >= len(self._row_index):
            self._render_summary()
            return

        row_type, key = self._row_index[self._selected]

        if row_type == 'ap' and key in self._scan.aps:
            self._render_ap_detail(self._scan.aps[key])
        elif row_type == 'sta' and key in self._scan.stations:
            self._render_sta_detail(self._scan.stations[key])
        else:
            self._render_summary()

    _UNKNOWN_VALUES = frozenset(
        {'', '-', '?', '---', '--- / ---', 'unknown', '<hidden>'})

    def _detail_header(self, title: str) -> str:
        return Text(f' \u2500 {title} \u2500').style(
            {'fg': self.style['accent'], 'bold': True})

    def _detail_field(self, label: str, value: str) -> str:
        """A detail row: muted label, bright value, muted placeholder."""
        value = sanitize_text(value)
        muted = self.style['plot_fg']
        value_fg = (muted if value in self._UNKNOWN_VALUES
                    else self.style['fg'])
        return (Text(f' {label:<11}').style({'fg': muted})
                + Text(value).style({'fg': value_fg}))

    def _render_ap_detail(self, ap) -> None:
        w = self._detail_win
        width_map = {0: '20 MHz', 1: '40 MHz', 2: '80 MHz', 3: '160 MHz'}
        if ap.max_rate and ap.n_ss:
            max_rate = f'{ap.max_rate:.0f} Mbps ({ap.n_ss}SS)'
        elif ap.max_rate:
            max_rate = f'{ap.max_rate:.0f} Mbps'
        else:
            max_rate = '-'
        sta_count = sum(1 for s in self._scan.stations.values()
                        if s.bssid == ap.bssid)

        w.append_line(self._detail_header('AP Detail'))
        w.append_line(self._detail_field('SSID:', ap.ssid or '<hidden>'))
        w.append_line(self._detail_field('BSSID:', ap.bssid))
        w.append_line(self._detail_field('Channel:', str(ap.channel or '?')))
        w.append_line(self._detail_field(
            'Width:', width_map.get(ap.chan_width, '?')))
        w.append_line('')
        w.append_line(self._detail_field('Security:', ap.security))
        w.append_line(self._detail_field('Cipher:', ap.cipher))
        w.append_line(self._detail_field('PMF:', ap.pmf))
        w.append_line('')
        w.append_line(self._detail_field('PHY:', ap.phy or '-'))
        w.append_line(self._detail_field('Max Rate:', max_rate))
        w.append_line(self._detail_field('Country:', ap.country or '-'))
        w.append_line(self._detail_field('Vendor:', ap.manufacturer or '-'))
        if ap.rx_gain > 0:
            w.append_line(self._detail_field(
                'Signal:', f'{ap.rx_gain:.0f} dB gain'))
        w.append_line('')
        w.append_line(self._detail_field('Beacons:', str(ap.beacon_count)))
        w.append_line(self._detail_field('Clients:', str(sta_count)))

    def _render_sta_detail(self, sta) -> None:
        w = self._detail_win
        w.append_line(self._detail_header('Station Detail'))
        w.append_line(self._detail_field('MAC:', sta.mac))
        vendor = _oui_lookup(sta.mac)
        w.append_line(self._detail_field('Vendor:', vendor or '-'))
        w.append_line(self._detail_field(
            'Channel:', str(sta.channel or '?')))
        if sta.rx_gain > 0:
            w.append_line(self._detail_field(
                'Signal:', f'{sta.rx_gain:.0f} dB gain'))
        w.append_line('')
        if sta.bssid in self._scan.aps:
            ap = self._scan.aps[sta.bssid]
            w.append_line(self._detail_field('AP:', ap.ssid or '<hidden>'))
            w.append_line(self._detail_field('', sta.bssid))
        elif sta.bssid:
            w.append_line(self._detail_field('AP:', sta.bssid))
        else:
            w.append_line(self._detail_field('AP:', 'unassociated'))
        w.append_line('')
        w.append_line(self._detail_field('Frames:', str(sta.frame_count)))
        if sta.probed_ssids:
            w.append_line('')
            w.append_line(Text(' Probes:').style(
                {'fg': self.style['plot_fg']}))
            for ssid in sta.probed_ssids:
                w.append_line(Text(f'   {sanitize_text(ssid)}').style(
                    {'fg': self.style['fg']}))

    def _render_summary(self) -> None:
        w = self._detail_win
        groups = self._scan.group_aps()
        w.append_line(self._detail_header('Scan Summary'))
        w.append_line(self._detail_field('APs:', str(len(self._scan.aps))))
        w.append_line(self._detail_field(
            'Stations:', str(len(self._scan.stations))))
        w.append_line(self._detail_field('Devices:', str(len(groups))))

    # ─── Navigation ───────────────────────────────────────────────────

    def _select_delta(self, d: int) -> None:
        if not self._row_index:
            return
        new = self._selected + d
        # Skip separator rows
        while 0 <= new < len(self._row_index) and self._row_index[new][0] == 'sep':
            new += (1 if d > 0 else -1)
        new = max(0, min(len(self._row_index) - 1, new))
        # If clamped onto a separator, step back
        if self._row_index[new][0] == 'sep' and new > 0:
            new -= 1
        self._selected = new
        if self.table:
            if self._selected < self.table.offset:
                self.table.scroll_to(self._selected)
            elif self._selected >= self.table.offset + self.table.visible_rows:
                self.table.scroll_to(
                    self._selected - self.table.visible_rows + 1)
        self._update_table()
        self._update_detail()

    def _select_to(self, idx: int) -> None:
        if not self._row_index:
            return
        self._selected = max(0, min(idx, len(self._row_index) - 1))
        if self._row_index[self._selected][0] == 'sep':
            # Move off separator
            if self._selected > 0:
                self._selected -= 1
            elif self._selected + 1 < len(self._row_index):
                self._selected += 1
        if self.table:
            self.table.scroll_to(self._selected)
        self._update_table()
        self._update_detail()

    def _page_up(self, _n: int) -> None:
        if self.table:
            self.table.scroll_page_up()
            self._selected = self.table.offset
            if self._row_index and self._row_index[self._selected][0] == 'sep':
                self._select_delta(1)
            else:
                self._update_table()
                self._update_detail()

    def _page_down(self, _n: int) -> None:
        if self.table:
            self.table.scroll_page_down()
            self._selected = min(
                len(self._row_index) - 1,
                self.table.offset + self.table.visible_rows - 1)
            if self._row_index and self._row_index[self._selected][0] == 'sep':
                self._select_delta(-1)
            else:
                self._update_table()
                self._update_detail()

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        last = lambda: len(self._row_index) - 1 if self._row_index else 0
        return [
            ('j', 'Select next row', lambda c: self._select_delta(c)),
            ('k', 'Select previous row', lambda c: self._select_delta(-c)),
            ('up', 'Select previous row', lambda c: self._select_delta(-1)),
            ('down', 'Select next row', lambda c: self._select_delta(1)),
            ('s', 'Start/stop channel scanner',
             lambda c: self.stop_scan() if self._scanning else self._start_scan()),
            ('home', 'Jump to first row', lambda c: self._select_to(0)),
            ('end', 'Jump to last row', lambda c: self._select_to(last())),
            ('page_up', 'Page up', lambda c: self._page_up(c)),
            ('page_down', 'Page down', lambda c: self._page_down(c)),
            ('g', 'Jump to first row', lambda c: self._select_to(0)),
            ('G', 'Jump to last row', lambda c: self._select_to(last())),
        ]

    def theme_changed(self, style: dict) -> None:
        self.style = style


class TrafficView(View):
    """WiFi traffic view — rolling chart + per-type breakdown table."""

    name = 'traffic'
    hints = 'j/k:select  g/G:top/bottom  c:clear  -/+:win'
    mode = DaemonMode.WIFI

    def __init__(self, tx_bar: WifiTxBar | None = None) -> None:
        super().__init__()
        self.style = DEFAULT_PALETTE
        self.table: Table | None = None
        self._label_win: Window | None = None
        self._chart_win: Window | None = None
        self._axis_win: Window | None = None
        self._bar_win: StyledWindow | None = None
        self._tx_bar = tx_bar or WifiTxBar()
        self._state = TrafficState()
        self._types: list[str] = []
        self._selected_idx = 0
        self._daemon = None
        self._drain_needed = False
        self._bucket_timer = 0.0
        self._bucket_time = 0.0
        self._dirty = False
        self._window_size = _DEFAULT_WINDOW

    # ─── Layout ───────────────────────────────────────────────────────

    def layout(self, container: StyledWindow) -> list[Window]:
        body, bar = container.hsplit(None, 1)
        chart_h = max(6, int(body.height * 0.35))
        axis_h = 1
        table_h = body.height - chart_h - axis_h
        if table_h < 3:
            table_h = 3
            chart_h = body.height - table_h - axis_h
        if chart_h < 4:
            chart_h = 4
            table_h = body.height - chart_h - axis_h

        parts = body.hsplit(chart_h, axis_h, table_h)
        chart_parts = parts[0].vsplit(5, None)

        self._label_win = StyledWindow(
            chart_parts[0].x, chart_parts[0].y,
            chart_parts[0].width, chart_parts[0].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['plot_fg']})
        self._chart_win = StyledWindow(
            chart_parts[1].x, chart_parts[1].y,
            chart_parts[1].width, chart_parts[1].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['accent']})
        self._axis_win = StyledWindow(
            parts[1].x, parts[1].y, parts[1].width, parts[1].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['plot_fg']})
        self._bar_win = StyledWindow(
            bar.x, bar.y, bar.width, bar.height,
            {'bg': self.style['header_bg'], 'fg': self.style['header_fg']})

        self.table = Table(
            parts[2],
            [None, 8, 8, 7, 8],
            header=['Type', 'Count', 'Rate', '%', 'FCS OK%'],
            header_style={
                'bg': self.style['header_bg'],
                'fg': self.style['header_fg'],
                'bold': True,
            },
            row_style={'bg': self.style['bg'], 'fg': self.style['fg']},
            alt_row_style={
                'bg': self.style['header_bg'], 'fg': self.style['fg']},
            highlight_style={
                'bg': self.style['accent'], 'fg': self.style['bg']},
        )
        for ci in (1, 2, 3, 4):
            self.table.set_column_format(ci, {'justify': 'right'})
        for ci in (2, 3):
            self.table.set_column_style(
                ci, {'fg': self.style['plot_fg']})

        self._update_table()
        self._render_chart()
        self._render_bar()

        windows: list[Window] = [
            self._label_win, self._chart_win, self._axis_win, self._bar_win]
        windows.extend(self.table.columns)
        if self.table.draw_header:
            windows.extend(self.table._header_columns)
        return windows

    # ─── Lifecycle ────────────────────────────────────────────────────

    def on_enter(self) -> None:
        self._drain_needed = True
        if self.config:
            w = self.config.get_int('wifi.traffic.window', _DEFAULT_WINDOW)
            if w != self._window_size:
                self._window_size = w
                self._state.resize(w)

    def config_changed(self, key: str, daemon) -> None:
        self._daemon = daemon
        if key in ('fpga_active', '_status'):
            fpga = daemon.get_state().get('fpga_active', '0') == '1'
            if fpga and daemon.get_state().get('agc_mode', 'manual') == 'manual':
                daemon.apply('gain', 'auto')
        self._render_bar()

    def config_updated(self, key: str, value: str) -> None:
        if key == 'wifi.traffic.window':
            try:
                w = int(value)
                if w != self._window_size:
                    self._window_size = w
                    self._state.resize(w)
                    self._dirty = True
            except ValueError:
                pass

    def theme_changed(self, style: dict) -> None:
        self.style = style
        self._render_bar()

    def _daemon_state(self) -> dict:
        daemon = self._daemon or self.daemon
        return daemon.get_state() if daemon is not None else {}

    def _render_bar(self) -> None:
        if self._bar_win is not None:
            self._tx_bar.render(self._bar_win, self.style,
                                self._daemon_state())

    # ─── Data ─────────────────────────────────────────────────────────

    def update(self, frame: dict) -> None:
        if frame.get('type') != 'wifi' and self._drain_needed:
            return
        self._drain_needed = False
        if frame.get('type') == 'wifi':
            self._state.update(frame)

    def tick(self, dt: float) -> None:
        self._bucket_timer += dt
        if self._bucket_timer >= 1.0:
            self._bucket_timer -= 1.0
            self._bucket_time += 1.0
            self._state.advance_bucket(self._bucket_time)
            self._dirty = True

        if self._dirty:
            self._dirty = False
            self._update_table()
        self._render_chart()
        self._render_bar()

    # ─── Table ────────────────────────────────────────────────────────

    _TYPE_EMPHASIS = {
        'deauth': 'warn',
        'disassoc': 'warn',
        'eapol': 'accent',
    }

    def _type_cell(self, name: str):
        """Colour security-relevant frame types; the selection outranks it."""
        role = self._TYPE_EMPHASIS.get(name)
        if role:
            return (name, {'fg': self.style[role], 'bold': True})
        return name

    def _update_table(self) -> None:
        if self.table is None:
            return
        totals = self._state.totals()
        self._types = []
        rows: list[list] = []

        # ALL row
        all_data = totals.get('ALL', {})
        fcs_all = all_data.get('fcs_ok_pct', 0.0)
        rows.append([
            self._type_cell('ALL'),
            str(all_data.get('count', 0)),
            f"{all_data.get('rate', 0):.1f}",
            '100%',
            f"{fcs_all:.0f}%" if (all_data.get('count', 0)) > 0 else '--',
        ])
        self._types.append('ALL')

        # Per-type rows
        for name, data in totals.items():
            if name == 'ALL':
                continue
            fcs = data.get('fcs_ok_pct', 0.0)
            rows.append([
                self._type_cell(name),
                str(data['count']),
                f"{data['rate']:.1f}",
                f"{data['pct']:.0f}%",
                f"{fcs:.0f}%" if data['count'] > 0 else '--',
            ])
            self._types.append(name)

        if not rows:
            rows = [['Listening...', '', '', '', '']]
            self._types = ['ALL']

        # Clamp selection
        self._selected_idx = max(0, min(self._selected_idx, len(self._types) - 1))

        # Highlight
        self.table.set_highlight(self._selected_idx if rows else None)
        self.table.update(rows)

    # ─── Chart ────────────────────────────────────────────────────────

    def _render_chart(self) -> None:
        if self._chart_win is None:
            return
        chart = self._chart_win
        buckets = self._state.buckets

        cw = chart.width
        ch = chart.height
        if ch < 2:
            chart.update_content('')
            self._update_y_labels(0)
            self._update_x_axis()
            return

        if not buckets:
            chart.update_content('')
            self._update_y_labels(0)
            self._update_x_axis()
            return

        type_name = self._selected_type_name()
        values = self._state.values_for_type(type_name)

        max_y = max(values) if values else 0
        if max_y == 0:
            max_y = 1
        ceil = max_y * 1.1

        plot = Plot(cw, ch, 0, 0, len(values) - 1, ceil)

        prev_x: float = 0.0
        prev_y: float = 0.0
        for i, val in enumerate(values):
            x = float(i)
            y = float(val)
            if i > 0:
                plot.line(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y

        chart.update_content(plot.draw())
        self._update_y_labels(ceil)
        self._update_x_axis()

    def _update_y_labels(self, max_y: float) -> None:
        if self._label_win is None:
            return
        lw = self._label_win
        h = lw.height
        lines = [' ' * lw.width for _ in range(h)]
        n_labels = max(2, h // 2)
        for i in range(n_labels):
            row = i * 2
            if row >= h:
                break
            frac = 1.0 - i / (n_labels - 1) if n_labels > 1 else 0.5
            val = round(frac * max_y)
            lines[row] = f'{val}'.rjust(lw.width)
        lw.update_buffer(lines)

    def _update_x_axis(self) -> None:
        if self._axis_win is None:
            return
        aw = self._axis_win
        w = aw.width
        data_s = len(self._state.buckets)
        if w < 10:
            aw.update_content('0'.ljust(w))
            return
        left = '0'
        right = f'{data_s}s'
        pad = w - len(left) - len(right)
        if pad < 2:
            aw.update_content(left.center(w))
        else:
            lpad = pad // 2
            rpad = pad - lpad
            aw.update_content(left + ' ' * lpad + ' ' * rpad + right)

    # ─── Selection ────────────────────────────────────────────────────

    def _selected_type_name(self) -> str:
        if self._types and 0 <= self._selected_idx < len(self._types):
            return self._types[self._selected_idx]
        return 'ALL'

    def _select_delta(self, d: int) -> None:
        if not self._types:
            return
        self._selected_idx = max(
            0, min(len(self._types) - 1, self._selected_idx + d))
        self._dirty = True

    def _select_to(self, idx: int) -> None:
        if not self._types:
            return
        self._selected_idx = max(0, min(len(self._types) - 1, idx))
        self._dirty = True

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        last = lambda: len(self._types) - 1 if self._types else 0
        return [
            ('j', 'Select next type', lambda c: self._select_delta(c)),
            ('k', 'Select previous type', lambda c: self._select_delta(-c)),
            ('up', 'Select previous type', lambda c: self._select_delta(-1)),
            ('down', 'Select next type', lambda c: self._select_delta(1)),
            ('home', 'Jump to first row', lambda c: self._select_to(0)),
            ('end', 'Jump to last row', lambda c: self._select_to(last())),
            ('g', 'Jump to first row', lambda c: self._select_to(0)),
            ('G', 'Jump to last row', lambda c: self._select_to(last())),
            ('-', 'Adjust time window down', lambda c: self._cycle_window(-1)),
            ('+', 'Adjust time window up', lambda c: self._cycle_window(1)),
            ('=', 'Adjust time window up', lambda c: self._cycle_window(1)),
            ('c', 'Reset chart history', lambda c: self._reset()),
        ]

    # ─── Window / Reset ────────────────────────────────────────────────

    def _cycle_window(self, direction: int) -> None:
        if not self.config:
            return
        try:
            idx = _WINDOW_SIZES.index(self._window_size)
        except ValueError:
            idx = _WINDOW_SIZES.index(_DEFAULT_WINDOW)
        idx = (idx + direction) % len(_WINDOW_SIZES)
        new_size = _WINDOW_SIZES[idx]
        if new_size != self._window_size:
            self._window_size = new_size
            self._state.resize(new_size)
            self.config.set('wifi.traffic.window', str(new_size))
            self._dirty = True

    def _reset(self) -> None:
        self._state.reset()
        self._bucket_time = 0.0
        self._bucket_timer = 0.0
        self._dirty = True

    def command(self, args: list[str]) -> tuple[str | None, str]:
        if not args:
            return (None, '')
        return (f'traffic: unknown: {args[0]}', '')

    def word_completions(self) -> list[str]:
        return []


_TX_USAGE = ('usage: tx beacon <ssid> [options] | '
             'tx frame <beacon|deauth|raw> ...')

_TX_COMMON_OPTIONS = frozenset({
    'rate', 'count', 'interval', 'cyclic', 'channel',
})
_TX_FRAME_OPTIONS = {
    'beacon': _TX_COMMON_OPTIONS | {
        'ssid', 'bssid', 'da', 'sa', 'seq', 'capability',
        'beacon-interval', 'ie',
    },
    'deauth': _TX_COMMON_OPTIONS | {'da', 'bssid', 'sa', 'reason', 'seq'},
    'raw': _TX_COMMON_OPTIONS | {'psdu-hex', 'psdu-file'},
}
_TX_FLAGS = frozenset({'cyclic'})
_TX_REPEATABLE = frozenset({'ie'})


def _tx_option_candidates(kind: str, text: str) -> list[str]:
    """Complete ``--options`` for one frame kind.

    ``text`` starts at the first argument after the command node, so any
    earlier arguments are kept in the replacement candidate.
    """
    options = _TX_FRAME_OPTIONS.get(kind, frozenset())
    token = text.rsplit(None, 1)[-1] if text and not text[-1].isspace() else ''
    if token and not token.startswith('--'):
        return []
    prefix = text[:len(text) - len(token)]
    return [prefix + '--' + name for name in sorted(options)
            if ('--' + name).startswith(token)]


def _parse_tx_options(args: list[str], allowed: frozenset[str],
                      frame_type: str) -> tuple[dict | None, list[str] | None,
                                                str | None]:
    opts: dict = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith('--'):
            positionals.append(arg)
            i += 1
            continue
        name = arg[2:]
        if name not in allowed:
            return None, None, f'unknown option for {frame_type}: {arg}'
        if name in _TX_FLAGS:
            opts[name] = True
            i += 1
            continue
        if i + 1 >= len(args):
            return None, None, f'{arg} requires a value'
        value = args[i + 1]
        if name in _TX_REPEATABLE:
            opts.setdefault(name, []).append(value)
        elif name in opts:
            return None, None, f'{arg} given more than once'
        else:
            opts[name] = value
        i += 2
    return opts, positionals, None


def _tx_int(opts: dict, name: str, default: int, minimum: int,
            maximum: int | None = None, *, message: str | None = None,
            base: int = 10) -> tuple[int | None, str | None]:
    if name not in opts:
        return default, None
    raw = opts[name]
    try:
        value = int(raw, base)
    except (TypeError, ValueError):
        return None, f'invalid --{name}: {raw}'
    if value < minimum or (maximum is not None and value > maximum):
        return None, message or f'--{name} out of range'
    return value, None


def _mac_value(text: str, label: str) -> tuple[bytes | None, str | None]:
    try:
        return parse_mac(text), None
    except ValueError:
        return None, f'invalid {label}: {text}'


def _tx_mac(opts: dict, name: str,
            default: bytes | None = None) -> tuple[bytes | None, str | None]:
    if name not in opts:
        return default, None
    return _mac_value(opts[name], f'--{name}')


def _hex_value(text: str, label: str) -> tuple[bytes | None, str | None]:
    cleaned = ''.join(text.replace(':', '').replace('-', '').split())
    try:
        data = bytes.fromhex(cleaned)
    except ValueError:
        return None, f'invalid {label}: {text}'
    if not data:
        return None, f'invalid {label}: {text}'
    return data, None


def _parse_ie(spec: str) -> tuple[bytes | None, str | None]:
    tag_text, sep, data_text = spec.partition(':')
    if not sep:
        return None, f'invalid --ie (expected TAG:HEX): {spec}'
    try:
        tag = int(tag_text, 0)
    except ValueError:
        return None, f'invalid --ie tag: {tag_text}'
    if not 0 <= tag <= 0xFF:
        return None, f'--ie tag out of range: {tag}'
    data, err = _hex_value(data_text, '--ie data')
    if err:
        return None, err
    if len(data) > 0xFF:
        return None, '--ie data is longer than 255 bytes'
    return bytes([tag, len(data)]) + data, None


def _parse_tx_schedule(opts: dict) -> tuple[tuple | None, str | None]:
    count_given = 'count' in opts
    interval_given = 'interval' in opts
    if 'cyclic' in opts and (count_given or interval_given):
        return None, 'count/interval cannot be combined with --cyclic'
    cyclic = 'cyclic' in opts or not (count_given or interval_given)

    rate, err = _tx_int(opts, 'rate', 6, 6, 54,
                        message='rate must be 6-54 Mbps')
    if err:
        return None, err
    count, err = _tx_int(opts, 'count', 1, 0,
                         message='count must be >= 0')
    if err:
        return None, err
    interval, err = _tx_int(opts, 'interval', 0, 0,
                            message='interval must be >= 0')
    if err:
        return None, err
    return (rate, count, interval, cyclic), None


def _resolve_tx_channel(frame_type: str, opts: dict,
                        daemon) -> tuple[int | None, int | None, str | None]:
    """Pick the channel and, for an explicit choice, the tune target.

    A beacon without ``--channel`` uses the current radio frequency, so its
    DS parameter set always matches what is actually on air.  An explicit
    channel is a request to tune first, for any frame type.
    """
    if 'channel' in opts:
        channel, err = _tx_int(opts, 'channel', 6, 0, 0xFF)
        if err:
            return None, None, err
        freq_mhz = CHANNEL_TO_FREQ.get(channel)
        if freq_mhz is None:
            return None, None, f'unknown WiFi channel: {channel}'
        return channel, freq_mhz * 1_000_000, None
    if frame_type != 'beacon':
        return None, None, None

    try:
        freq_hz = int(float(daemon.get_state().get('freq', '0')))
    except (TypeError, ValueError):
        freq_hz = 0
    channel = _freq_mhz_to_channel(freq_hz / 1e6) if freq_hz else 0
    if channel == 0:
        return (None, None,
                'radio is not tuned to a WiFi channel; pass --channel N')
    return channel, None, None


def _build_frame(frame_type: str, opts: dict,
                 positionals: list[str]) -> tuple[bytes | None, dict | None,
                                                    str | None]:
    if frame_type == 'beacon':
        if len(positionals) > 1:
            return None, None, f'too many arguments for beacon: {positionals[1:]}'
        ssid_arg = positionals[0] if positionals else None
        ssid_opt = opts.get('ssid')
        if ssid_arg is not None and ssid_opt is not None:
            return (None, None,
                    'specify the SSID as an argument or with --ssid, not both')
        ssid = ssid_arg if ssid_arg is not None else (ssid_opt or 'SHESHA')

        bssid, err = _tx_mac(opts, 'bssid', DEFAULT_BSSID)
        if err:
            return None, None, err
        da, err = _tx_mac(opts, 'da', BROADCAST)
        if err:
            return None, None, err
        sa, err = _tx_mac(opts, 'sa')
        if err:
            return None, None, err
        channel, err = _tx_int(opts, 'channel', 6, 0, 0xFF)
        if err:
            return None, None, err
        seq, err = _tx_int(opts, 'seq', 0, 0, 0xFFF)
        if err:
            return None, None, err
        capability, err = _tx_int(opts, 'capability', 0x0401, 0, 0xFFFF,
                                  base=0)
        if err:
            return None, None, err
        beacon_interval, err = _tx_int(
            opts, 'beacon-interval', 100, 0, 0xFFFF)
        if err:
            return None, None, err

        ies = []
        for spec in opts.get('ie', []):
            ie, err = _parse_ie(spec)
            if err:
                return None, None, err
            ies.append(ie)

        try:
            psdu = build_beacon(
                ssid=ssid, bssid=bssid, da=da, sa=sa, channel=channel,
                seq=seq, capability=capability,
                beacon_interval=beacon_interval, ies=ies)
        except ValueError as exc:
            return None, None, str(exc)
        return psdu, {'frame_type': 'beacon', 'ssid': ssid,
                      'bssid': bssid}, None

    if frame_type == 'deauth':
        if len(positionals) > 2:
            return None, None, f'too many arguments for deauth: {positionals[2:]}'
        if positionals and ('da' in opts or 'bssid' in opts):
            return (None, None,
                    'specify the addresses as arguments or with '
                    '--da/--bssid, not both')
        if positionals:
            da, err = _mac_value(positionals[0], '--da')
            if err:
                return None, None, err
            bssid = None
            if len(positionals) == 2:
                bssid, err = _mac_value(positionals[1], '--bssid')
                if err:
                    return None, None, err
        else:
            da, err = _tx_mac(opts, 'da')
            if err:
                return None, None, err
            bssid, err = _tx_mac(opts, 'bssid')
            if err:
                return None, None, err
        if da is None:
            return None, None, 'deauth requires a destination address (--da)'
        if bssid is None:
            return None, None, 'deauth requires a BSSID (--bssid)'

        sa, err = _tx_mac(opts, 'sa')
        if err:
            return None, None, err
        reason, err = _tx_int(opts, 'reason', 7, 0, 0xFFFF)
        if err:
            return None, None, err
        seq, err = _tx_int(opts, 'seq', 0, 0, 0xFFF)
        if err:
            return None, None, err

        try:
            psdu = build_deauth(da, bssid, sa=sa, reason=reason, seq=seq)
        except ValueError as exc:
            return None, None, str(exc)
        return psdu, {'frame_type': 'deauth', 'da': da,
                      'bssid': bssid, 'reason': reason}, None

    if frame_type == 'raw':
        if len(positionals) > 1:
            return None, None, f'too many arguments for raw: {positionals[1:]}'
        if positionals and ('psdu-hex' in opts or 'psdu-file' in opts):
            return (None, None,
                    'specify the PSDU once (argument, --psdu-hex, '
                    'or --psdu-file)')
        if positionals:
            data, err = _hex_value(positionals[0], 'PSDU')
        elif 'psdu-hex' in opts:
            data, err = _hex_value(opts['psdu-hex'], 'PSDU')
        elif 'psdu-file' in opts:
            path = os.path.expanduser(opts['psdu-file'])
            try:
                data, err = Path(path).read_bytes(), None
            except OSError as exc:
                return None, None, f'cannot read {path}: {exc}'
        else:
            return (None, None,
                    'raw frame requires a PSDU (argument, --psdu-hex, '
                    'or --psdu-file)')
        if err:
            return None, None, err
        try:
            psdu = build_raw(data)
        except ValueError as exc:
            return None, None, str(exc)
        return psdu, {'frame_type': 'raw'}, None

    return None, None, f'unknown frame type: {frame_type}'


class WifiApp(App):
    """WiFi scan app."""

    name = 'wifi'
    description = 'scan, traffic, inject'
    default_view = 'scan'

    def __init__(self) -> None:
        super().__init__()
        self._tx_bar = WifiTxBar()
        self.views = {'scan': ScanView(self._tx_bar),
                      'traffic': TrafficView(self._tx_bar)}
        self.commands['tx'] = self._tx_command
        self.commands['record'] = self._record_command
        self.commands['record_stop'] = self._record_stop_command

    def command_nodes(self, run, completions) -> list[Command]:
        def tx_options(kind):
            return lambda text: _tx_option_candidates(kind, text)

        return [
            Command('tx',
                    run=lambda a: run('tx', a),
                    description='Transmit WiFi frames',
                    complete=lambda text: completions('tx', text),
                    children={
                        'beacon': Command(
                            'beacon',
                            run=lambda a: run('tx', ['beacon', *a]),
                            description='Transmit beacons',
                            usage='<ssid> [opts]',
                            complete=tx_options('beacon')),
                        'frame': Command(
                            'frame',
                            run=lambda a: run('tx', ['frame', *a]),
                            description='Transmit crafted frames',
                            usage='<beacon|deauth|raw> ...',
                            children={
                                'beacon': Command(
                                    'beacon',
                                    run=lambda a: run(
                                        'tx', ['frame', 'beacon', *a]),
                                    description='Crafted beacon',
                                    usage='[<ssid>] [opts]',
                                    complete=tx_options('beacon')),
                                'deauth': Command(
                                    'deauth',
                                    run=lambda a: run(
                                        'tx', ['frame', 'deauth', *a]),
                                    description='Deauthentication frame',
                                    usage='<da> <bssid> [opts]',
                                    complete=tx_options('deauth')),
                                'raw': Command(
                                    'raw',
                                    run=lambda a: run(
                                        'tx', ['frame', 'raw', *a]),
                                    description='Raw PSDU in hex',
                                    usage='<hex> [opts]',
                                    complete=tx_options('raw')),
                            }),
                    }),
            Command('record',
                    run=lambda a: run('record', a),
                    description='Record frames to a pcap file',
                    usage='[--eapol] [file]',
                    complete=lambda text: completions('record', text),
                    children={'stop': Command(
                        'stop', run=lambda a: run('record_stop', a),
                        description='Stop recording')}),
        ]

    def command_completions(self, name: str, text: str) -> list[str]:
        tokens = text.split()
        if name == 'tx':
            if not tokens:
                return ['beacon', 'frame']
            if len(tokens) == 1 and not text.endswith(' '):
                return [op for op in ('beacon', 'frame')
                        if op.startswith(tokens[0])]
            if tokens[0] == 'frame':
                return [f'frame {kind}' for kind in _TX_FRAME_OPTIONS
                        if f'frame {kind}'.startswith(text)]
            return []
        if name == 'record':
            if tokens and tokens[0] == 'stop':
                return []
            partial = tokens[-1] if text and not text[-1].isspace() else ''
            prefix = text[:len(text) - len(partial)]
            paths = complete_path(partial)
            if partial and not os.path.dirname(os.path.expanduser(partial)):
                paths = [p[2:] if p.startswith('.' + os.sep) else p
                         for p in paths]
            return [prefix + path for path in paths]
        return []

    @classmethod
    def config_keys(cls) -> dict[str, KeySpec]:
        return {
            'wifi.scan.start_channel': KeySpec('int', default=1, min=1, max=177),
            'wifi.scan.stop_channel':  KeySpec('int', default=177, min=1, max=177),
            'wifi.scan.dwell':         KeySpec('float', default=2.0, min=0.1, max=60.0),
            'wifi.scan.max_age':       KeySpec('float', default=120.0, min=1.0, max=3600.0),
            'wifi.traffic.window':     KeySpec('int', default=_DEFAULT_WINDOW,
                                               min=1, max=600),
        }

    # ─── TX Commands ─────────────────────────────────────────────────

    def _tx_command(self, args: list[str], daemon: 'Daemon') -> tuple[str | None, str]:
        if not args:
            return (_TX_USAGE, '')
        op = args[0]
        if op == 'beacon':
            return self._frame(['beacon'] + args[1:], daemon)
        if op == 'frame':
            return self._frame(args[1:], daemon)
        return (f'unknown tx op: {op}', '')

    def _frame(self, args: list[str], daemon: 'Daemon') -> tuple[str | None, str]:
        if not args:
            return (_TX_USAGE, '')
        frame_type = args[0]
        allowed = _TX_FRAME_OPTIONS.get(frame_type)
        if allowed is None:
            return (f'unknown frame type: {frame_type}', '')

        opts, positionals, err = _parse_tx_options(
            args[1:], allowed, frame_type)
        if err:
            return (err, '')

        schedule, err = _parse_tx_schedule(opts)
        if err:
            return (err, '')
        rate, count, interval, cyclic = schedule

        channel, tune_hz, err = _resolve_tx_channel(
            frame_type, opts, daemon)
        if err:
            return (err, '')
        if channel is not None and frame_type == 'beacon':
            opts = dict(opts)
            opts['channel'] = str(channel)

        psdu, meta, err = _build_frame(frame_type, opts, positionals)
        if err or meta is None:
            return (err or 'frame build failed', '')

        info = WifiTxInfo(rate_mbps=rate, psdu_len=len(psdu), **meta)
        scan_view = self.views.get('scan')
        if scan_view is not None:
            scan_view.stop_scan()
        try:
            if tune_hz is not None:
                daemon.apply('freq', str(tune_hz))
            daemon.wifi_tx(rate, psdu, repeat=count,
                           interval_us=interval, cyclic=cyclic)
            self._tx_bar.info = info
        except Exception as e:
            return (str(e), '')
        return (None, meta.get('ssid') or frame_type)

    # ─── Record Commands ──────────────────────────────────────────────

    @staticmethod
    def _record_command(args: list[str], daemon: 'Daemon') -> tuple[str | None, str]:
        parts: list[str] = []
        eapol_only = False
        for arg in args:
            if arg == '--eapol':
                eapol_only = True
            elif not arg.startswith('--'):
                parts.append(arg)
        filename = ' '.join(parts) or None

        if filename is None:
            ts = time.strftime('%Y%m%d_%H%M%S')
            filename = f'capture_{ts}.pcap'

        return (None, filename)

    @staticmethod
    def _record_stop_command(args: list[str], daemon: 'Daemon') -> tuple[str | None, str]:
        return (None, '')
