"""Viz app: waterfall / spectrum visualization.

Provides FFT visualisation modes (waterfall, spectrum, etc.)
and owns dot-prefixed config keys (viz.waterfall.*).
"""
from __future__ import annotations
from collections import deque
from typing import Callable
import math
import time

from .app import App, View
from .config import KeySpec
from .fmt import format_freq
from .pytui import StyledWindow, Window, Plot, PixelCanvas, Text
from .theme import DEFAULT_PALETTE
from ..protocol import DaemonMode, unpack_iq

_FFT_SIZES = [512, 1024, 2048, 4096, 8192]
_AVG_MODES = ['off', 'window', 'decay']
_FPS_VALUES = [10, 20, 30, 60]
_DEFAULT_FFT_SIZE = 4096
_DEFAULT_FPS = 20
_MAX_FRAME_BUFFER = 256


_MIN_DB_SPAN = 10.0


class WaterfallView(View):
    name = 'waterfall'
    hints = "'\":fps  a:avg  s:fft  -/=:floor  _/+:ceil"
    mode = DaemonMode.WATERFALL

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        # Injectable so the headless snapshot tool can pace the waterfall
        # deterministically instead of racing the wall clock.
        self._clock = clock
        self._psd_win: StyledWindow | None = None
        self._wf_win: StyledWindow | None = None
        self._freq_win: StyledWindow | None = None
        self._db_win: StyledWindow | None = None
        self._bar_win: StyledWindow | None = None
        self._history: deque[list[float]] = deque(maxlen=200)
        self._frame_buffer: list[list[float]] = []
        self._decay_state: list[float] | None = None
        self._db_floor = -100.0
        self._db_ceil = -20.0
        self._range_locked = False
        self._range_seeded = False
        self._fps = _DEFAULT_FPS
        self._fft_size = _DEFAULT_FFT_SIZE
        self._avg_mode = 'off'
        self._avg_value = 8
        self._center_mhz: float | None = None
        self._bw_mhz: float | None = None
        self._last_row_time = 0.0

        # Stream health, shown on the bottom bar.  ``_obs_fps`` is the
        # measured PSD delivery rate; ``_target_fps`` is what the daemon is
        # paced to.  ``_coverage`` is the fraction of the sample stream that
        # reached an FFT -- below 1.0 the spectrum is gappy in time.
        self._last_psd_ts: float | None = None
        self._last_frame_at = 0.0
        self._obs_fps = 0.0
        self._target_fps = 0
        self._coverage: float | None = None
        self._workers = 0

        self.style: dict = DEFAULT_PALETTE
        self._cmap = DEFAULT_PALETTE['colormap']
        self._windows: list[Window] = []

    def config_updated(self, key: str, value: str) -> None:
        if key == 'viz.waterfall.fps':
            try:
                self._fps = max(1, int(value))
            except ValueError:
                pass
        elif key == 'viz.waterfall.avg_mode':
            if value in _AVG_MODES:
                self._avg_mode = value
                self._decay_state = None
                self._frame_buffer.clear()
        elif key == 'viz.waterfall.avg_value':
            try:
                self._avg_value = max(1, int(float(value)))
            except ValueError:
                pass
        elif key == 'viz.waterfall.fft_size':
            try:
                self._fft_size = int(value)
            except ValueError:
                pass
        elif key == 'viz.waterfall.floor':
            try:
                self._set_range(floor=float(value))
            except ValueError:
                pass
        elif key == 'viz.waterfall.ceil':
            try:
                self._set_range(ceil=float(value))
            except ValueError:
                pass

    # ─── Layout ──────────────────────────────────────────────────────────

    def layout(self, container: StyledWindow) -> list[Window]:
        bar_h = 1
        psd_h = max(4, int(container.height * 0.35))
        freq_h = 1
        wf_h = container.height - psd_h - freq_h - bar_h
        if wf_h < 2:
            wf_h = 2
            psd_h = container.height - wf_h - freq_h - bar_h

        parts = container.hsplit(psd_h, freq_h, wf_h, bar_h)
        psd_parts = parts[0].vsplit(5, None)
        self._db_win = StyledWindow(
            psd_parts[0].x, psd_parts[0].y, psd_parts[0].width, psd_parts[0].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['plot_fg']})
        self._psd_win = StyledWindow(
            psd_parts[1].x, psd_parts[1].y, psd_parts[1].width, psd_parts[1].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['accent']})
        self._freq_win = StyledWindow(
            parts[1].x, parts[1].y, parts[1].width, parts[1].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['plot_fg']})
        self._wf_win = StyledWindow(
            parts[2].x, parts[2].y, parts[2].width, parts[2].height,
            {'bg': self.style['plot_bg']})
        self._bar_win = StyledWindow(
            parts[3].x, parts[3].y, parts[3].width, parts[3].height,
            {'bg': self.style['header_bg'], 'fg': self.style['header_fg']})

        wf_depth = self._wf_win.height * 2
        self._history = deque(maxlen=wf_depth)
        for _ in range(wf_depth):
            self._history.append([self._db_floor] * self._wf_win.width)

        self._update_freq_axis()
        self._render_bar()
        self._windows = [self._db_win, self._psd_win, self._freq_win,
                         self._wf_win, self._bar_win]
        return self._windows

    # ─── Keybinds ────────────────────────────────────────────────────────

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        return [
            ("'", 'Cycle FPS rate',
             lambda n: self._cycle_or_set('viz.waterfall.fps', n, _FPS_VALUES,
                                          min_val=1, default=_DEFAULT_FPS)),
            ('a', 'Toggle averaging', lambda n: self._handle_avg(n)),
            ('s', 'Cycle FFT size',
             lambda n: self._cycle_or_set('viz.waterfall.fft_size', n, _FFT_SIZES,
                                          min_val=64, default=_DEFAULT_FFT_SIZE,
                                          clamp_to_pow2=True)),
            ('-', 'Adjust floor down', lambda n: self._adjust_range(floor_delta=-5 * n)),
            ('=', 'Adjust floor up', lambda n: self._adjust_range(floor_delta=5 * n)),
            ('_', 'Adjust ceiling down', lambda n: self._adjust_range(ceil_delta=-5 * n)),
            ('+', 'Adjust ceiling up', lambda n: self._adjust_range(ceil_delta=5 * n)),
        ]

    def _cycle_or_set(self, key: str, n: int, choices: list,
                      min_val: int = 1, default: int | None = None,
                      clamp_to_pow2: bool = False) -> None:
        if not self.config:
            return
        current = self._get_state_val(key, default)
        if n <= 1:
            try:
                idx = choices.index(current)
                nxt = choices[(idx + 1) % len(choices)]
            except ValueError:
                nxt = choices[0]
            applied = str(nxt)
        else:
            val = max(min_val, n)
            if clamp_to_pow2:
                val = _nearest_pow2(val)
            applied = str(val)
        # Update our own state too.  config_updated is otherwise only reached
        # when the shell sets the key, so a keybind would advance the stored
        # value while _get_state_val kept returning the old one -- and every
        # later press recomputed the same "next".
        self.apply_config(key, applied)
        self.config_updated(key, applied)

    def _handle_avg(self, n: int) -> None:
        if not self.config:
            return
        if n <= 1:
            idx = (_AVG_MODES.index(self._avg_mode) + 1) % len(_AVG_MODES)
            mode = _AVG_MODES[idx]
            self.apply_config('viz.waterfall.avg_mode', mode)
            self.config_updated('viz.waterfall.avg_mode', mode)
        else:
            value = str(max(1, n))
            self.apply_config('viz.waterfall.avg_value', value)
            self.config_updated('viz.waterfall.avg_value', value)

    def _get_state_val(self, key: str, default: int) -> int:
        attr_map = {
            'viz.waterfall.fps': self._fps,
            'viz.waterfall.fft_size': self._fft_size,
            'viz.waterfall.avg_value': self._avg_value,
        }
        return attr_map.get(key, default)

    # ─── Commands / Completions ──────────────────────────────────────────

    def command(self, args: list[str]) -> tuple[str | None, str]:
        if not args:
            return (None, '')
        verb = args[0]
        if verb in ('floor', 'ceil'):
            if len(args) < 2:
                return (f'{verb}: missing value', '')
            try:
                value = float(args[1])
            except ValueError:
                return (f'invalid {verb}: {args[1]}', '')
            ok = (self._set_range(floor=value) if verb == 'floor'
                  else self._set_range(ceil=value))
            if not ok:
                return (f'{verb} must leave at least {_MIN_DB_SPAN:g} dB '
                        'between floor and ceiling', '')
            self._save_range()
        elif verb == 'auto':
            self._range_locked = False
            self._range_seeded = False
        else:
            return (f'unknown: {verb}', '')
        return (None, '')

    def command_hints(self) -> list[tuple[str, str]]:
        return [('floor <dB>', 'Lock the display floor'),
                ('ceil <dB>', 'Lock the display ceiling'),
                ('auto', 'Return to automatic ranging')]

    def completions(self, tokens: list[str]) -> list[str]:
        return ['auto', 'floor', 'ceil']

    # ─── Theme / Config from daemon ──────────────────────────────────────

    def theme_changed(self, style: dict) -> None:
        self.style = style
        self._cmap = style.get('colormap', DEFAULT_PALETTE['colormap'])

    def config_changed(self, key: str, daemon) -> None:
        state = daemon.get_state()
        if key == 'freq':
            freq = state.get('freq')
            if freq is not None:
                try:
                    self._center_mhz = float(freq) / 1e6
                    self._update_freq_axis()
                except ValueError:
                    pass
        elif key == 'sample_rate':
            rate = state.get('sample_rate')
            if rate is not None:
                try:
                    self._bw_mhz = float(rate) / 1e6
                    self._update_freq_axis()
                except ValueError:
                    pass
        elif key == 'target_fps':
            self.set_target_fps(int(float(state.get('target_fps', 0) or 0)))
        elif key == 'psd_coverage':
            raw = state.get('psd_coverage')
            self.set_coverage(float(raw) if raw not in (None, '') else None)
        elif key == 'workers':
            self.set_workers(int(float(state.get('workers', 0) or 0)))

    def set_target_fps(self, fps: int) -> None:
        """Set the daemon's paced PSD rate, for the bottom bar."""
        self._target_fps = fps

    def set_coverage(self, coverage: float | None) -> None:
        """Set the fraction of the sample stream that reached an FFT."""
        self._coverage = coverage

    def set_workers(self, workers: int) -> None:
        """Set the running PSD producer worker count, for the bottom bar."""
        self._workers = workers

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def on_enter(self) -> None:
        self._load_range()
        self._load_viz_config()

    def on_exit(self) -> None:
        self._frame_buffer.clear()
        self._decay_state = None

    def _load_viz_config(self) -> None:
        if not self.config:
            return
        self._fps = self.config.get_int('viz.waterfall.fps', _DEFAULT_FPS)
        self._fft_size = self.config.get_int('viz.waterfall.fft_size', _DEFAULT_FFT_SIZE)
        mode = self.config.get('viz.waterfall.avg_mode')
        if mode in _AVG_MODES:
            self._avg_mode = mode
        self._avg_value = self.config.get_int('viz.waterfall.avg_value', 8)
        self._last_row_time = self._clock()

    def tick(self, dt: float) -> None:
        if self.animator:
            self.animator.tick(dt)
        self._render_bar()

    def _track_stream(self, ts: float | None) -> None:
        """Measure PSD delivery rate and liveness from frame timestamps."""
        if ts is not None:
            if self._last_psd_ts is not None and ts > self._last_psd_ts:
                instant = 1.0 / (ts - self._last_psd_ts)
                if self._obs_fps:
                    self._obs_fps += 0.25 * (instant - self._obs_fps)
                else:
                    self._obs_fps = instant
            self._last_psd_ts = ts
        self._last_frame_at = time.time()

    # ─── Update (data feed) ──────────────────────────────────────────────

    def update(self, frame: dict) -> None:
        bins = frame.get('bins')
        if not bins:
            return

        self._track_stream(frame.get('ts'))

        if not self._range_locked and bins:
            dc = max(bins)
            df = min(bins)
            if dc - df >= 10:
                target_floor = max(df, dc - 85.0)
                target_ceil = dc
                if not self._range_seeded:
                    self._db_floor = target_floor
                    self._db_ceil = target_ceil
                    self._range_seeded = True
                else:
                    alpha = 0.15
                    self._db_floor += alpha * (target_floor - self._db_floor)
                    self._db_ceil += alpha * (target_ceil - self._db_ceil)

        new_center = frame.get('center_freq_mhz')
        if new_center is not None and new_center > 0:
            self._center_mhz = new_center

        if self._avg_mode == 'decay':
            alpha = max(0.01, min(0.99, self._avg_value / 100.0))
            if self._decay_state is None or len(self._decay_state) != len(bins):
                self._decay_state = list(bins)
            else:
                om_alpha = 1.0 - alpha
                self._decay_state = [
                    alpha * b + om_alpha * s
                    for b, s in zip(bins, self._decay_state)
                ]

        if not self._wf_win:
            return

        self._frame_buffer.append(bins)
        if len(self._frame_buffer) > _MAX_FRAME_BUFFER:
            del self._frame_buffer[0]

        now = self._clock()
        row_interval = 1.0 / max(1, self._fps)
        if now - self._last_row_time < row_interval:
            return

        self._last_row_time = now

        if not self._frame_buffer:
            return

        if self._avg_mode == 'window':
            n = max(1, self._avg_value)
            recent = self._frame_buffer[-n:]
            avg_bins = _mean_bins(recent)
        elif self._avg_mode == 'decay':
            avg_bins = list(self._decay_state) if self._decay_state else self._frame_buffer[-1]
        else:
            avg_bins = self._frame_buffer[-1]

        self._frame_buffer.clear()

        width = self._wf_win.width
        scaled = self._scale_bins(avg_bins, width)
        self._history.append(scaled)

        self._render_waterfall()
        self._render_psd(avg_bins)
        self._update_freq_axis()

    # ─── Scaling / colormap / rendering ──────────────────────────────────

    def _scale_bins(self, bins: list[float], target: int) -> list[float]:
        n = len(bins)
        if n <= target:
            return bins + [self._db_floor] * (target - n)
        scaled = []
        for col in range(target):
            start = int(col * n / target)
            end = int((col + 1) * n / target)
            scaled.append(max(bins[start:end]))
        return scaled

    def _db_to_color(self, db: float) -> tuple[int, int, int]:
        span = self._db_ceil - self._db_floor
        t = 0.0 if span <= 0 else (db - self._db_floor) / span
        t = max(0.0, min(1.0, t))
        return self._cmap[int(t * 255)]

    def _render_waterfall(self) -> None:
        if not self._wf_win:
            return
        wf = self._wf_win
        hist = list(self._history)
        rows = wf.height
        depth = rows * 2
        canvas = PixelCanvas(wf.width, depth, self._cmap[0])

        for y in range(depth):
            row_idx = len(hist) - depth + y
            if 0 <= row_idx < len(hist):
                row = hist[row_idx]
                for x in range(min(wf.width, len(row))):
                    canvas.set(x, y, self._db_to_color(row[x]))

        lines = canvas.draw().split('\n')
        buf = [wf.justify_line(line) for line in lines]
        wf.update_buffer(buf)

    def _render_bar(self) -> None:
        """Draw PSD producer health on the bottom bar.

        Coverage below 99% means the display is missing signal in time --
        a short burst can land between frames and never show -- so it is
        warned on.  The rate is the measured PSD delivery over the daemon's
        target, with liveness stated separately.  The worker count is the
        running producer thread count.
        """
        if not self._bar_win:
            return
        bw = self._bar_win
        bg = self.style['header_bg']
        fg = self.style['header_fg']
        dim = self.style['dim']
        warn = self.style['warn']

        def seg(text: str, color=fg) -> str:
            return Text(text).style({'fg': color, 'bg': bg})

        if self._coverage is None:
            coverage = seg(' coverage ', dim) + seg('-', dim)
        else:
            pct = self._coverage * 100.0
            coverage = (seg(' coverage ', dim)
                        + seg(f'{pct:5.1f}%', fg if pct >= 99.0 else warn))

        live = bool(self._last_frame_at) and (
            time.time() - self._last_frame_at) < 1.0
        measured = f'{self._obs_fps:.0f}' if self._obs_fps else '--'
        if self._target_fps:
            rate = f'{measured}/{self._target_fps} Hz'
        else:
            rate = f'{measured} Hz'
        fps = (seg('  fps ', dim) + seg(f'{rate} ', fg)
               + (seg('ok', fg) if live else seg('stalled', warn)))

        workers = seg('  workers ', dim) + seg(
            str(self._workers) if self._workers else '-', fg)

        bw.update_buffer([bw.justify_line(coverage + fps + workers, 'left')])

    def _render_psd(self, bins: list[float]) -> None:
        if not self._psd_win:
            return
        pw = self._psd_win
        ceil = max(self._db_ceil, self._db_floor + _MIN_DB_SPAN)
        plot = Plot(pw.width, pw.height, 0, self._db_floor, len(bins), ceil)
        step = max(1, len(bins) // (pw.width * 2))
        x1, y1 = 0, bins[0]
        for i in range(step, len(bins), step):
            x2, y2 = i, bins[i]
            plot.line(x1, y1, x2, y2)
            x1, y1 = x2, y2
        pw.update_content(plot.draw())
        self._update_db_labels()

    def _update_db_labels(self) -> None:
        if not self._db_win:
            return
        dw = self._db_win
        h = dw.height
        lines = [' ' * dw.width for _ in range(h)]
        n_labels = max(2, h // 2)
        for i in range(n_labels):
            row = i * 2
            if row >= h:
                break
            t = 1.0 - i / (n_labels - 1) if n_labels > 1 else 0.5
            db = self._db_floor + t * (self._db_ceil - self._db_floor)
            lines[row] = f'{int(round(db))}'.rjust(dw.width)
        dw.update_buffer(lines)

    def _update_freq_axis(self, center_mhz: float | None = None,
                          bw_mhz: float | None = None) -> None:
        if not self._freq_win:
            return
        cm = center_mhz if center_mhz is not None else self._center_mhz
        bm = bw_mhz if bw_mhz is not None else self._bw_mhz
        fw = self._freq_win
        if cm is None or bm is None or cm <= 0 or bm <= 0:
            fw.update_content('--' if fw.width >= 2 else '-')
            return
        low_hz = int((cm - bm / 2) * 1e6)
        high_hz = int((cm + bm / 2) * 1e6)
        center_hz = int(cm * 1e6)
        center = format_freq(center_hz)
        left = format_freq(low_hz)
        right = format_freq(high_hz)
        pad = fw.width - len(left) - len(center) - len(right)
        if pad < 2:
            line = center.center(fw.width)
        else:
            lpad = pad // 2
            rpad = pad - lpad
            line = left + ' ' * lpad + center + ' ' * rpad + right
        fw.update_content(line[:fw.width])

    # ─── Range control ───────────────────────────────────────────────────

    def _set_range(self, floor: float | None = None,
                   ceil: float | None = None) -> bool:
        """Apply a new bound only when the floor stays below the ceiling."""
        new_floor = self._db_floor if floor is None else floor
        new_ceil = self._db_ceil if ceil is None else ceil
        if new_ceil - new_floor < _MIN_DB_SPAN:
            return False
        self._db_floor = new_floor
        self._db_ceil = new_ceil
        self._range_locked = True
        return True

    def _adjust_range(self, floor_delta: float = 0, ceil_delta: float = 0) -> None:
        self._db_floor += floor_delta
        self._db_ceil += ceil_delta
        if self._db_floor >= self._db_ceil - _MIN_DB_SPAN:
            self._db_floor = self._db_ceil - _MIN_DB_SPAN
        self._range_locked = True
        self._save_range()

    def _load_range(self) -> None:
        if not self.config:
            return
        cf = self.config.get_float('viz.waterfall.floor')
        cc = self.config.get_float('viz.waterfall.ceil')
        if cf and cc and cc - cf >= _MIN_DB_SPAN:
            self._db_floor = cf
            self._db_ceil = cc
            self._range_locked = True

    def _save_range(self) -> None:
        if not self.config:
            return
        self.config.set('viz.waterfall.floor', str(self._db_floor))
        self.config.set('viz.waterfall.ceil', str(self._db_ceil))


_HEAT_REF = math.log1p(24.0)   # density that saturates the colormap
_CONST_DEFAULT_BITS = 8
_CONST_DEFAULT_DECAY = 0.85
_CONST_RANGE_STEP = 0.05
_CONST_FRAME_VALUES = [10, 20, 30, 60]
_CONST_DEFAULT_FPS = 30


class ConstView(View):
    """Raw-IQ constellation: a decaying density heatmap."""

    name = 'const'
    mode = DaemonMode.IQ
    hints = "'\":fps  s:bits  -/=:zoom  a:auto"

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._clock = clock
        self.style: dict = DEFAULT_PALETTE
        self._cmap = DEFAULT_PALETTE['colormap']
        self._plot_win: StyledWindow | None = None
        self._bar_win: StyledWindow | None = None
        self._windows: list[Window] = []
        self._heat: list[float] = []
        self._w = 0
        self._h = 0
        # Pane size in pixels and the offset of the square data plane
        # within it.  Kept separate from _w/_h so the plane stays square
        # while the pane does not.
        self._canvas_w = 0
        self._canvas_h = 0
        self._ox = 0
        self._oy = 0
        self._peak = 0.0
        self._range: float | None = None
        self._decay = _CONST_DEFAULT_DECAY
        self._bits = _CONST_DEFAULT_BITS
        self._wire_bits: int | None = None
        self._fps = _CONST_DEFAULT_FPS
        self._last_frame_time = 0.0
        self._last_frame_at = 0.0
        self._last_ts: float | None = None
        self._obs_fps = 0.0
        self._frames_processed = 0

    # ─── Config / lifecycle ──────────────────────────────────────────

    def _load_config(self) -> None:
        if not self.config:
            return
        self._bits = self.config.get_int('viz.const.bits', _CONST_DEFAULT_BITS)
        if self._bits not in (8, 12):
            self._bits = _CONST_DEFAULT_BITS
        self._decay = self.config.get_float('viz.const.decay',
                                            _CONST_DEFAULT_DECAY)
        rng = self.config.get_float('viz.const.range', 0.0)
        self._range = rng if rng > 0 else None
        self._fps = self.config.get_int('viz.const.fps', _CONST_DEFAULT_FPS)

    def config_updated(self, key: str, value: str) -> None:
        if key == 'viz.const.bits':
            try:
                bits = int(value)
            except ValueError:
                return
            if bits in (8, 12):
                if self.active and self.daemon:
                    self._set_bits(bits)
                else:
                    self._bits = bits
        elif key == 'viz.const.decay':
            try:
                self._decay = max(0.0, min(0.99, float(value)))
            except ValueError:
                pass
        elif key == 'viz.const.range':
            try:
                rng = float(value)
                self._range = rng if rng > 0 else None
            except ValueError:
                pass
        elif key == 'viz.const.fps':
            try:
                self._fps = max(1, int(value))
            except ValueError:
                pass

    def _set_bits(self, bits: int) -> bool:
        """Adopt a requested width only when the daemon accepts it."""
        if self.daemon and not self.daemon.set_iq_bits(bits):
            return False
        self._bits = bits
        return True

    def on_enter(self) -> None:
        self._load_config()
        self._set_bits(self._bits)

    def theme_changed(self, style: dict) -> None:
        self.style = style
        self._cmap = style.get('colormap', DEFAULT_PALETTE['colormap'])

    # ─── Layout ──────────────────────────────────────────────────────

    def layout(self, container: StyledWindow) -> list[Window]:
        bar_h = 1
        plot_h = max(1, container.height - bar_h)
        parts = container.hsplit(plot_h, bar_h)
        self._plot_win = StyledWindow(
            parts[0].x, parts[0].y, parts[0].width, parts[0].height,
            {'bg': self.style['plot_bg'], 'fg': self.style['plot_fg']})
        self._bar_win = StyledWindow(
            parts[1].x, parts[1].y, parts[1].width, parts[1].height,
            {'bg': self.style['header_bg'], 'fg': self.style['header_fg']})
        # Equal I/Q scale: one pixel per column, two per row, and terminal
        # cells are about 2:1 tall, so a physically round ring needs a
        # square pixel grid.  Center that square in the pane and pad the
        # longer dimension, otherwise the window shape turns the circle
        # into an oval and masks real I/Q gain or phase error.
        self._canvas_w = self._plot_win.width
        self._canvas_h = self._plot_win.height * 2
        side = max(2, min(self._canvas_w, self._canvas_h))
        self._ox = (self._canvas_w - side) // 2
        self._oy = (self._canvas_h - side) // 2
        self._w = side
        self._h = side
        self._heat = [0.0] * (side * side)
        self._windows = [self._plot_win, self._bar_win]
        self._render_bar()
        return self._windows

    # ─── Keybinds / commands ─────────────────────────────────────────

    def _cycle_bits(self, n: int) -> None:
        bits = 12 if self._bits == 8 else 8
        self.apply_config('viz.const.bits', str(bits))
        self.config_updated('viz.const.bits', str(bits))

    def _zoom(self, delta: float) -> None:
        base = self._range if self._range is not None else max(self._peak, 0.05)
        self._range = max(0.01, min(1.0, base + delta))
        if self.config:
            self.config.set('viz.const.range', str(self._range))
        self._render_bar()

    def _cycle_fps(self, n: int) -> None:
        if n <= 1:
            idx = (_CONST_FRAME_VALUES.index(self._fps)
                   if self._fps in _CONST_FRAME_VALUES else -1)
            nxt = _CONST_FRAME_VALUES[(idx + 1) % len(_CONST_FRAME_VALUES)]
        else:
            nxt = max(1, n)
        self.apply_config('viz.const.fps', str(nxt))
        self.config_updated('viz.const.fps', str(nxt))
        self._render_bar()

    def _key_defs(self):
        return [
            ("'", 'Cycle FPS rate', lambda n: self._cycle_fps(n)),
            ('s', 'Toggle 8/12-bit samples', lambda n: self._cycle_bits(n)),
            ('-', 'Zoom out', lambda n: self._zoom(_CONST_RANGE_STEP * n)),
            ('=', 'Zoom in', lambda n: self._zoom(-_CONST_RANGE_STEP * n)),
            ('a', 'Toggle autoscale', lambda n: self._toggle_range()),
        ]

    def _toggle_range(self) -> None:
        """Freeze the current automatic range, or return to auto."""
        if self._range is None:
            self._range = max(self._peak, 0.05)
        else:
            self._range = None
        if self.config:
            self.config.set('viz.const.range',
                            str(self._range if self._range is not None else 0))
        self._render_bar()

    def _reset_range(self) -> None:
        self._range = None
        if self.config:
            self.config.set('viz.const.range', '0')
        self._render_bar()

    def command(self, args: list[str]) -> tuple[str | None, str]:
        if not args:
            return (None, '')
        verb = args[0]
        if verb in ('bits', 'range'):
            if len(args) < 2:
                return (f'{verb}: missing value', '')
            self.config_updated(f'viz.const.{verb}', args[1])
        elif verb == 'auto':
            self._reset_range()
        else:
            return (f'unknown: {verb}', '')
        return (None, '')

    def command_hints(self) -> list[tuple[str, str]]:
        return [('bits <8|12>', 'Set sample bit depth'),
                ('range <0-1>', 'Set the constellation range'),
                ('auto', 'Return to automatic scaling')]

    def completions(self, tokens: list[str]) -> list[str]:
        return ['auto', 'bits', 'range']

    # ─── Data ────────────────────────────────────────────────────────

    def update(self, frame: dict) -> None:
        if frame.get('type') != 'iq':
            return
        wire_bits = frame.get('bits')
        if wire_bits in (8, 12):
            self._wire_bits = wire_bits
        if not self._heat:
            return
        self._last_frame_at = time.time()
        self._track_stream(frame.get('ts'))

        now = self._clock()
        if now - self._last_frame_time < 1.0 / max(1, self._fps):
            return
        self._last_frame_time = now

        pts = unpack_iq(frame.get('samples', b''), frame.get('bits', self._bits))
        if not pts:
            return

        peak = 0.0
        for z in pts:
            m = max(abs(z.real), abs(z.imag))
            if m > peak:
                peak = m
        self._peak = max(peak, self._peak * 0.99)

        r = self._range if self._range is not None else max(self._peak, 0.05)
        inv = 1.0 / (2.0 * r)
        heat = self._heat
        decay = self._decay
        w = self._w
        h = self._h
        if decay > 0.0:
            for i in range(len(heat)):
                heat[i] *= decay
        for z in pts:
            ix = math.floor((z.real + r) * inv * (w - 1))
            iy = math.floor((r - z.imag) * inv * (h - 1))
            if 0 <= ix < w and 0 <= iy < h:
                heat[iy * w + ix] += 1.0

        self._frames_processed += 1
        self._render_plot()
        self._render_bar()

    def _track_stream(self, ts: float | None) -> None:
        """EMA of the incoming IQ frame rate, from daemon timestamps."""
        if ts is None:
            return
        if self._last_ts is not None and ts > self._last_ts:
            instant = 1.0 / (ts - self._last_ts)
            if self._obs_fps:
                self._obs_fps += 0.25 * (instant - self._obs_fps)
            else:
                self._obs_fps = instant
        self._last_ts = ts

    # ─── Rendering ───────────────────────────────────────────────────

    def _render_plot(self) -> None:
        if not self._plot_win:
            return
        w = self._w
        h = self._h
        W = self._canvas_w
        H = self._canvas_h
        ox = self._ox
        oy = self._oy
        bg = self.style['plot_bg']
        axis = self.style['plot_fg']
        cx = (w - 1) // 2
        cy = (h - 1) // 2
        data = [bg] * (W * H)
        for y in range(h):
            row = (oy + y) * W + ox
            heat_row = y * w
            for x in range(w):
                v = self._heat[heat_row + x]
                if v > 0.0:
                    t = min(1.0, math.log1p(v) / _HEAT_REF)
                    data[row + x] = self._cmap[int(t * 255)]
                elif x == cx or y == cy:
                    data[row + x] = axis
        canvas = PixelCanvas(W, H, bg)
        canvas.set_data(data)
        self._plot_win.update_content(canvas.draw())

    def _render_bar(self) -> None:
        if not self._bar_win:
            return
        bw = self._bar_win
        bg = self.style['header_bg']
        fg = self.style['header_fg']
        dim = self.style['dim']
        warn = self.style['warn']

        def seg(text: str, color=fg) -> str:
            return Text(text).style({'fg': color, 'bg': bg})

        rng = self._range if self._range is not None else max(self._peak, 0.05)
        label = 'auto' if self._range is None else 'lock'
        live = bool(self._last_frame_at) and (time.time() - self._last_frame_at) < 1.0
        measured = f'{self._obs_fps:.0f}' if self._obs_fps else '--'
        shown_bits = self._wire_bits if self._wire_bits is not None else self._bits
        bar = (seg(' iq ', dim) + seg(f'{shown_bits}-bit ', fg)
               + seg('  range ', dim) + seg(f'{rng:.3f} ', fg)
               + seg(f'({label}) ', dim)
               + seg('  peak ', dim) + seg(f'{self._peak:.3f}', fg)
               + seg('  fps ', dim) + seg(f'{measured}/{self._fps} Hz ', fg)
               + (seg('ok', fg) if live else seg('stalled', warn)))
        bw.update_buffer([bw.justify_line(bar, 'left')])


class VizApp(App):
    name = 'viz'
    description = 'waterfall, constellation'
    default_view = 'waterfall'

    def __init__(self) -> None:
        super().__init__()
        self.views = {'waterfall': WaterfallView(), 'const': ConstView()}

    @classmethod
    def config_keys(cls) -> dict[str, KeySpec]:
        return {
            'viz.waterfall.fft_size':  KeySpec('fft_size', default=4096),
            'viz.waterfall.window':    KeySpec('window_type', default='hann',
                                                allowed=['rect', 'hann', 'blackman_harris', 'flat_top']),
            'viz.waterfall.accum':     KeySpec('accum_mode', default='max',
                                                allowed=['none', 'max', 'avg']),
            'viz.waterfall.fps':       KeySpec('int', default=20, min=1, max=60),
            'viz.waterfall.avg_mode':  KeySpec('str', default='off',
                                                allowed=['off', 'window', 'decay']),
            'viz.waterfall.avg_value': KeySpec('int', default=8, min=1, max=100),
            'viz.waterfall.floor':     KeySpec('float', default=-100.0),
            'viz.waterfall.ceil':      KeySpec('float', default=-20.0),
            'viz.const.bits':  KeySpec('int', default=8, min=8, max=12),
            'viz.const.decay': KeySpec('float', default=0.85, min=0.0, max=0.99),
            'viz.const.range': KeySpec('float', default=0.0, min=0.0, max=1.0),
            'viz.const.fps':   KeySpec('int', default=30, min=1, max=60),
        }


def _nearest_pow2(n: int) -> int:
    if n <= 0:
        return 1
    n -= 1
    n |= n >> 1
    n |= n >> 2
    n |= n >> 4
    n |= n >> 8
    n |= n >> 16
    n += 1
    return n


def _mean_bins(frames: list[list[float]]) -> list[float]:
    if not frames:
        return []
    n = len(frames[0])
    result = [0.0] * n
    for frame in frames:
        for i in range(min(n, len(frame))):
            result[i] += frame[i]
    inv = 1.0 / len(frames)
    return [v * inv for v in result]
