"""FM app: broadcast FM receiver and transmitter.

The RX view shows a classic-radio tuning dial (``|----|----|`` with a ``^``
caret under the tuned frequency), a live oscilloscope-style trace of the
demodulated audio, and an RDS station-info panel (placeholder until RDS
decode lands).

The TX view streams an audio file over FM.  Audio loading (ffmpeg decode)
runs in a background thread so it never blocks the 60 fps event loop.
Streaming uses a tick-driven state machine: each ``tick()`` pushes one chunk
if the daemon is ready, checks backpressure, and updates telemetry widgets.

The band and modulation are data, not literals (:class:`Band`), so a future
AM toggle (or an :mod:`fm.am` view) reuses the same dial rendering against a
different frequency range and step.  Firmware only emits FM today.
"""
from __future__ import annotations

import os
import threading
from array import array
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
import math
import time
from typing import Callable, TYPE_CHECKING

from .app import App, View
from .completion import complete_path, expand_paths, has_glob
from .config import KeySpec
from .fmt import format_freq, parse_tune_freq
from .pytui import (StyledWindow, Window, Plot, PixelCanvas, Text, Keyboard,
                    display_width, sanitize_text)
from .theme import DEFAULT_PALETTE
from ..bands import BANDS as SPECTRUM_BANDS, render_band_labels
from ..protocol import DaemonMode
from ..audio import AudioPlayer
from ..fm_audio import (
    AudioLoadError, audio_file_error, load_audio, prepare_fm_audio)
from ..rds import pty_name, pty_text, ps_text, rds_from_tags
from ..mp3 import TrackTags, probe, read_tags

if TYPE_CHECKING:
    from .daemon import Daemon


@dataclass(frozen=True)
class Band:
    """A broadcast band: frequency range, dial marks, and fine-tune step.

    All frequencies are in Hz.  ``major_hz`` is the spacing between major
    tick marks; ``label_every_hz`` (a multiple of ``major_hz``) is how often
    a numeric label is drawn; ``fine_hz`` is the ``[``/``]`` tuning step.
    """

    mod: str
    lo_hz: int
    hi_hz: int
    major_hz: int
    label_every_hz: int
    fine_hz: int

    def label(self, hz: int) -> str:
        """Short dial label at a tick frequency (e.g. ``"96"`` or ``"1000"``)."""
        if hz >= 10_000_000:
            s = f'{hz / 1e6:.1f}'
            s = s.rstrip('0').rstrip('.')
            return s
        return f'{hz / 1e3:.0f}'


BAND_FM = Band('fm', 87_500_000, 108_000_000, 1_000_000, 5_000_000, 100_000)
BAND_AM = Band('am', 530_000, 1_710_000, 100_000, 200_000, 10_000)

BANDS = {'fm': BAND_FM, 'am': BAND_AM}

# AD9361 tunable range — hard floor and ceiling for all dial operations.
FREQ_MIN_HZ = 70_000_000
FREQ_MAX_HZ = 6_000_000_000

# Default sliding-window width when the frequency is outside the band.
_WINDOW_HZ = 10_000_000


def dial_window(freq_hz: float, band: Band) -> tuple[int, int, int, int]:
    """Choose the visible dial window for a tuned frequency.

    Returns ``(lo_hz, hi_hz, major_hz, label_every_hz)``.

    Inside the band: returns the band's own range and tick spacing.
    Outside the band: returns a sliding window centered on *freq_hz*
    with 1 MHz major ticks and 5 MHz labels, clamped to the AD9361 range.
    """
    if band.lo_hz <= freq_hz <= band.hi_hz:
        return (band.lo_hz, band.hi_hz, band.major_hz, band.label_every_hz)

    half = _WINDOW_HZ // 2
    lo = int(freq_hz) - half
    hi = int(freq_hz) + half

    # Clamp to AD9361 range, shifting the window to preserve its width.
    if lo < FREQ_MIN_HZ:
        lo = FREQ_MIN_HZ
        hi = lo + _WINDOW_HZ
    if hi > FREQ_MAX_HZ:
        hi = int(FREQ_MAX_HZ)
        lo = hi - _WINDOW_HZ

    return (lo, hi, 1_000_000, 5_000_000)


def render_dial(width: int, freq_hz: float | None, band: Band, *,
                lo_hz: int | None = None, hi_hz: int | None = None,
                major_hz: int | None = None,
                label_every_hz: int | None = None) -> tuple[str, str, str]:
    """Render a classic-radio dial for `width` columns.

    Returns (ticks, caret, labels): three strings each `width` long.  The
    current frequency is mapped linearly onto the visible window; a single
    ``^`` in the caret line marks its position, with the tuned frequency
    printed beside it — to the right of the caret in the left half of the
    dial, to the left in the right half, so the label always sits in the
    larger of the two gaps.

    When *lo_hz*/*hi_hz* are given they define the visible window (which
    may differ from the band range for out-of-band frequencies).  When
    omitted, the band's own range is used.
    """
    if lo_hz is None:
        lo_hz = band.lo_hz
    if hi_hz is None:
        hi_hz = band.hi_hz
    if major_hz is None:
        major_hz = band.major_hz
    if label_every_hz is None:
        label_every_hz = band.label_every_hz

    if width < 1:
        return ('', '', '')

    def x_of(hz: float) -> int:
        frac = (hz - lo_hz) / (hi_hz - lo_hz)
        return int(round(frac * (width - 1)))

    ticks = [' '] * width
    labels = [' '] * width
    caret = [' '] * width

    hz = math.ceil(lo_hz / major_hz) * major_hz
    while hz <= hi_hz:
        x = x_of(hz)
        if 0 <= x < width:
            ticks[x] = '|'
            if hz % label_every_hz == 0:
                text = band.label(hz)
                start = max(0, min(x - len(text) // 2, width - len(text)))
                for i, ch in enumerate(text):
                    labels[start + i] = ch
        hz += major_hz

    if freq_hz is None:
        marker = '--'
        start = max(0, (width - len(marker)) // 2)
        for i, ch in enumerate(marker):
            if start + i < width:
                caret[start + i] = ch
        return (''.join(ticks), ''.join(caret), ''.join(labels))

    cx = x_of(freq_hz)
    cx = max(0, min(width - 1, cx))
    caret[cx] = '^'

    text = format_freq(int(freq_hz))
    if cx < width / 2:
        start = cx + 2  # one space after the caret
    else:
        start = cx - len(text) - 1  # end one space before the caret
    for i, ch in enumerate(text):
        pos = start + i
        if 0 <= pos < width:
            caret[pos] = ch

    return (''.join(ticks), ''.join(caret), ''.join(labels))


def _pad(lines: list[str], width: int, height: int) -> list[str]:
    """Pad or truncate a list of rendered lines to fill a fixed-height pane."""
    while len(lines) < height:
        lines.append(' ' * width)
    return lines[:height]


def _render_dial_styled(win: 'StyledWindow', freq_hz: float | None, band: Band,
                        style: dict) -> None:
    """Draw a styled frequency dial into a window.

    Tick marks and labels are dimmed, the caret is accented and bold, and
    spectrum band annotations use the primary foreground.  Both the RX and
    TX views delegate here so the visual hierarchy stays consistent.
    """
    if freq_hz is None:
        lo, hi = band.lo_hz, band.hi_hz
        major, label_every = band.major_hz, band.label_every_hz
    else:
        lo, hi, major, label_every = dial_window(freq_hz, band)
    ticks, caret, labels = render_dial(
        win.width, freq_hz, band,
        lo_hz=lo, hi_hz=hi,
        major_hz=major, label_every_hz=label_every)
    annotation = render_band_labels(SPECTRUM_BANDS, lo, hi, win.width)

    dim = style['dim']
    accent = style['accent']
    fg = style['fg']

    lines = [
        win.justify_line(Text(labels).style({'fg': dim})),
        win.justify_line(Text(ticks).style({'fg': dim})),
        win.justify_line(Text(caret).style({'fg': accent, 'bold': True})),
        win.justify_line(Text(annotation).style({'fg': fg})),
    ]
    win.update_buffer(_pad(lines, win.width, win.height))


_TRACE_CLIP = 0.95

# Samples in the scope window.  Kept as a fixed count so the trace spans the
# same time regardless of terminal width.
_WAVEFORM_WINDOW = 4096


def trace_color(peak: float, style: dict) -> tuple[int, int, int]:
    """Pick a scope trace colour from the audio peak level.

    Colour carries one diagnostic: ``warn`` when the deviation is being
    overdriven, which is visible overmodulation.  There is deliberately
    no quiet state.  An empty channel demodulates to loud hiss (peak
    0.3-0.5 in practice, gain-independent), so a low peak means silent
    audio or an unmodulated carrier, never an absent station.
    """
    if peak >= _TRACE_CLIP:
        return style['warn']
    return style['accent']


class FmRxView(View):
    """FM broadcast receiver: dial + audio waveform + RDS placeholder."""

    name = 'rx'
    hints = '[/]:fine tune  h/l:1 MHz  g:gain  ?:help'
    mode = DaemonMode.FM

    def __init__(self, player_factory: Callable[[int], object] | None = AudioPlayer) -> None:
        super().__init__()
        self.style = DEFAULT_PALETTE
        self._dial_win: StyledWindow | None = None
        self._plot_win: StyledWindow | None = None
        self._rds_win: StyledWindow | None = None
        self._daemon: Daemon | None = None
        self._band = BAND_FM
        self._freq_hz: int | None = None
        self._audio_rate = 43750
        self._player_factory = player_factory
        self._player: object | None = None
        # Waveform history is indexed by accepted-sample count, not by
        # arrival order.  The playhead is a cursor into that timeline.
        self._buf: deque[float] = deque()
        self._buf_start = 0
        self._accepted = 0
        self._freq_dirty = True
        self._rds = None
        self._rds_dirty = False

    # ─── Layout ─────────────────────────────────────────────────────────

    def layout(self, container: StyledWindow) -> list[Window]:
        dial_h = 4
        rds_h = 5
        plot_h = container.height - dial_h - rds_h
        if plot_h < 4:
            plot_h = 4
            rds_h = max(3, container.height - dial_h - plot_h)

        dial_region, plot_region, rds_region = container.hsplit(
            dial_h, plot_h, rds_h)

        self._dial_win = StyledWindow(
            dial_region.x, dial_region.y, dial_region.width, dial_region.height,
            {'bg': self.style['bg'], 'fg': self.style['fg']})
        self._plot_win = StyledWindow(
            plot_region.x, plot_region.y, plot_region.width, plot_region.height,
            {'bg': self.style['plot_bg'], 'fg': self.style['accent']})
        self._rds_win = StyledWindow(
            rds_region.x, rds_region.y, rds_region.width, rds_region.height,
            {'bg': self.style['bg'], 'fg': self.style['fg']})

        self._render_dial()
        self._render_rds()

        return [self._dial_win, self._plot_win, self._rds_win]

    # ─── Lifecycle ──────────────────────────────────────────────────────

    def on_enter(self) -> None:
        self._freq_dirty = True

    def on_exit(self) -> None:
        if self._player is not None:
            self._player.close()
            self._player = None

    def theme_changed(self, style: dict) -> None:
        self.style = style

    def config_changed(self, key: str, daemon: 'Daemon') -> None:
        self._daemon = daemon
        if key == 'freq':
            freq = daemon.get_state().get('freq')
            if freq is not None:
                try:
                    self._freq_hz = int(float(freq))
                    self._freq_dirty = True
                except (ValueError, TypeError):
                    pass

    # ─── Data ───────────────────────────────────────────────────────────

    def update(self, frame: dict) -> None:
        if frame.get('type') != 'fm':
            return
        audio = frame.get('audio') or []
        rate = frame.get('audio_rate')
        if rate and rate != self._audio_rate:
            self._audio_rate = rate
            if self._player is not None:
                self._player.close()
                self._player = None
        if self._player is not None and getattr(
                self._player, 'player_gone', False):
            self._player.close()
            self._player = None
        if self._player is None and self._player_factory is not None:
            self._player = self._player_factory(self._audio_rate)
            self._drain_stale()

        accepted = True
        if self._player is not None:
            accepted = self._player.push(list(audio))
        if accepted is not False:
            self._buf.extend(audio)
            self._accepted += len(audio)

        rds = frame.get('rds')
        if rds is not None:
            self._rds = rds
            self._rds_dirty = True

    def tick(self, dt: float) -> None:
        super().tick(dt)
        if self._freq_dirty:
            self._freq_dirty = False
            self._render_dial()
        if self._rds_dirty:
            self._rds_dirty = False
            self._render_rds()
        self._render_plot()

    # ─── Commands / keybinds ────────────────────────────────────────────

    def command(self, args: list[str]) -> tuple[str | None, str]:
        if not args:
            return (None, '')
        if args[0] in BANDS:
            # Future: AM/FM modulation switch.  Firmware is FM-only today.
            return (None, '')
        try:
            hz = parse_tune_freq(args[0])
        except ValueError:
            return (f'invalid frequency: {args[0]}', '')
        if not FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ:
            return (f'frequency out of range: {format_freq(hz)}', '')
        self._freq_hz = hz
        self._freq_dirty = True
        self.apply_config('freq', str(hz))
        return (None, '')

    def word_completions(self) -> list[str]:
        return []

    def command_hints(self) -> list[tuple[str, str]]:
        return [('<freq>', 'Tune to a frequency (e.g. 100.1)')]

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        return [
            ('[', 'Fine tune down', lambda c: self._fine_tune(-c)),
            (']', 'Fine tune up', lambda c: self._fine_tune(c)),
        ]

    def _fine_tune(self, n: int) -> None:
        if not self.config or self._freq_hz is None:
            return
        new = self._freq_hz + n * self._band.fine_hz
        new = max(FREQ_MIN_HZ, min(FREQ_MAX_HZ, new))
        self._freq_hz = new
        self._freq_dirty = True
        self.apply_config('freq', str(new))

    # ─── Rendering ──────────────────────────────────────────────────────

    def _render_dial(self) -> None:
        if self._dial_win is not None:
            _render_dial_styled(self._dial_win, self._freq_hz,
                                self._band, self.style)

    def _render_rds(self) -> None:
        """Draw decoded RDS state.

        Every value has a placeholder state, because the decoder fills one
        segment at a time: PS arrives two characters per group, RadioText
        four, and a station may never send PTYN at all. ``_`` marks a
        character that has not been received yet; ``---`` marks a field the
        decoder has no data for.
        """
        if self._rds_win is None:
            return
        w = self._rds_win
        dim = self.style['dim']
        fg = self.style['fg']
        accent = self.style['accent']
        s = self._rds

        def field(label: str, value: str):
            used = fg if value else dim
            return (Text(f' {label:<11}').style({'fg': dim})
                    + Text(sanitize_text(value) or '---').style({'fg': used}))

        if s is None:
            fields = [('Station:', ''), ('Program:', ''),
                      ('PTY / PI:', ''), ('RadioText:', '')]
            status = ''
        else:
            ps = ps_text(s.ps, s.ps_mask)
            ptyn = s.ptyn.strip(' ') if s.ptyn_complete else ''
            pty = pty_text(s.pty) if s.pi_valid else ''
            pi = s.pi_text if s.pi_valid else ''
            rt = s.rt.rstrip() if s.rt_valid else ''
            fields = [
                ('Station:', ps),
                ('Program:', ptyn or (pty_name(s.pty)
                                      if s.pi_valid and s.pty else '')),
                ('PTY / PI:', f'{pty} / {pi}'.strip(' /')),
                ('RadioText:', rt),
            ]
            status = 'RDS \u25cf' if s.synced else 'RDS \u25cb'
            if s.ta:
                status += ' TA'

        header = (Text(f' {self._band.mod.upper()} \u00b1'
                       f'{self._band.fine_hz // 1_000} kHz').style(
                           {'fg': accent, 'bold': True})
                  + Text(f'   {status}').style({'fg': dim}))
        lines = [w.justify_line(header)]
        for label, value in fields:
            lines.append(w.justify_line(field(label, value)))
        w.update_buffer(_pad(lines, w.width, w.height))

    def _downsample(self, samples: list[float], target: int) -> list[float]:
        n = len(samples)
        if n <= target:
            return samples
        stride = n / target
        out = []
        for i in range(target):
            out.append(samples[int(i * stride)])
        return out

    def _waveform_samples(self) -> list[float]:
        """The samples currently audible, oldest first.

        With a player clock this is the window ending at the played sample.
        Without one, it is the newest window, as before.
        """
        played = None
        if self._player is not None:
            played_fn = getattr(self._player, 'played_samples', None)
            if played_fn is not None:
                played = played_fn()
        end = self._accepted if played is None else max(
            0, min(self._accepted, int(played)))
        self._prune_history(end)
        lo = max(self._buf_start, end - _WAVEFORM_WINDOW)
        hi = end
        if hi <= lo:
            hi = self._accepted
            lo = max(self._buf_start, hi - _WAVEFORM_WINDOW)
        start = lo - self._buf_start
        return list(islice(self._buf, start, start + (hi - lo)))

    def _prune_history(self, end: int) -> None:
        """Drop samples no longer needed to draw the window.

        A lagging playhead still needs the samples between it and the
        accepted frontier, so only the left edge is trimmed.
        """
        cutoff = end - _WAVEFORM_WINDOW
        drop = cutoff - self._buf_start
        if drop <= 0:
            return
        drop = min(drop, len(self._buf))
        for _ in range(drop):
            self._buf.popleft()
        self._buf_start += drop

    def _render_plot(self) -> None:
        if self._plot_win is None:
            return
        w = self._plot_win
        if w.width < 2 or w.height < 2 or not self._buf:
            w.update_content('')
            return
        samples = self._waveform_samples()
        if not samples:
            w.update_content('')
            return
        m = w.width * 2
        if len(samples) > m:
            samples = self._downsample(samples, m)
        n = len(samples)
        plot = Plot(w.width, w.height, 0, -1.0, n - 1 or 1, 1.0)
        prev_x, prev_y = 0.0, float(samples[0])
        for i in range(1, n):
            x, y = float(i), float(samples[i])
            plot.line(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y
        peak = max(abs(s) for s in samples)
        self._plot_win.update_style({
            'bg': self.style['plot_bg'],
            'fg': trace_color(peak, self.style),
        })
        self._plot_win.update_content(plot.draw())

    def _drain_stale(self) -> None:
        """Drop waveform history on (re)start so the trace starts fresh."""
        self._buf.clear()
        self._buf_start = 0
        self._accepted = 0


# ─── FM TX pacing ────────────────────────────────────────────────────────
# How far ahead of realtime the client may run.  Enough to absorb jitter,
# small enough that daemon memory stays bounded.
_TX_LEAD_SEC = 1.0
_TX_CHUNK_SEC = 0.1

# Now-playing panel geometry; the playlist takes whatever is left.
_TX_PLAYER_H = 12

# Seconds moved per < / > seek press (a count prefix scales it).
_TX_SEEK_SEC = 5.0

# Telemetry is a blocking daemon round-trip; four polls a second is plenty
# for a 60 fps status strip.
_TX_STATS_INTERVAL = 0.25

# ─── Spectrum visualiser (now-playing panel) ─────────────────────────────
# Winamp-style bar chart: the outgoing audio's FFT split into log-spaced
# bands, drawn on a half-block canvas.  Half the panel width, five rows.
_SPECTRUM_ROWS = 5
_SPECTRUM_F_MIN_HZ = 40.0
_SPECTRUM_F_MAX_HZ = 16_000.0
_SPECTRUM_FALL_PER_S = 3.0
_SPECTRUM_FFT_MAX = 1024
_SPECTRUM_WIDE_BINS = 4
# Calibrated against Webamp's linear peak meter: top-four average, then a
# fixed ceiling.  Full-scale tones pin; quiet material stays low.
_SPECTRUM_LEVEL_GAIN = 3.0
_SPECTRUM_LEVEL_CLAMP = 15.0
# Winamp drops a peak cap 3/256 px on the first frame, then accelerates
# the drop by 1.1x each frame, so the cap hangs before it falls.
_SPECTRUM_PEAK_DROP = 3.0 / (256.0 * 15.0)
_SPECTRUM_PEAK_ACCEL = 1.1


def _fft_size(n_samples: int) -> int:
    """Largest power of two <= *n_samples*, capped for render speed."""
    n = 1
    while n * 2 <= min(n_samples, _SPECTRUM_FFT_MAX):
        n *= 2
    return n


def _fft_magnitudes(samples: Sequence[float], n: int) -> list[float]:
    """Magnitudes of a Hann-windowed radix-2 FFT of the first *n* samples.

    Returns the one-sided spectrum (``n // 2`` bins).  Pure Python: the
    client has no numpy, and *n* is capped small enough to run each frame.
    """
    re = [0.0] * n
    im = [0.0] * n
    denom = max(1, n - 1)
    for i in range(n):
        re[i] = samples[i] * (0.5 - 0.5 * math.cos(2.0 * math.pi * i / denom))

    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            re[i], re[j] = re[j], re[i]

    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wlen_r, wlen_i = math.cos(ang), math.sin(ang)
        half = length // 2
        for start in range(0, n, length):
            wr, wi = 1.0, 0.0
            for k in range(half):
                a = start + k
                b = a + half
                vr = re[b] * wr - im[b] * wi
                vi = re[b] * wi + im[b] * wr
                re[b] = re[a] - vr
                im[b] = im[a] - vi
                re[a] += vr
                im[a] += vi
                wr, wi = wr * wlen_r - wi * wlen_i, wr * wlen_i + wi * wlen_r
        length <<= 1

    return [math.hypot(re[k], im[k]) for k in range(n // 2)]


def _equalize_table(n_bins: int) -> list[float]:
    """Winamp's per-bin weighting: bass is attenuated, treble passes."""
    bias = 0.04
    table = []
    for k in range(n_bins):
        table.append(math.log10(
            1.0 + bias + (k + 1) * (9.0 - bias) / n_bins))
        bias /= 1.0025
    return table


def _spectrum_levels(samples: Sequence[float], rate: int,
                     n_bars: int) -> list[float]:
    """Map audio samples to *n_bars* log-spaced 0..1 band levels.

    Follows Winamp's linear peak meter: average the strongest few bins in
    each band after equalisation, then clamp against a fixed ceiling.  A
    full-scale tone pins the band; quieter material stays near the floor.
    Bass is bar 0.
    """
    if rate <= 0:
        return [0.0] * n_bars
    n = _fft_size(len(samples))
    if n < 8:
        return [0.0] * n_bars
    mags = _fft_magnitudes(samples[-n:], n)
    eq = _equalize_table(len(mags))

    f_max = min(_SPECTRUM_F_MAX_HZ, rate / 2.0)
    ratio = f_max / _SPECTRUM_F_MIN_HZ
    bin_hz = rate / n
    levels = []
    for b in range(n_bars):
        f0 = _SPECTRUM_F_MIN_HZ * ratio ** (b / n_bars)
        f1 = _SPECTRUM_F_MIN_HZ * ratio ** ((b + 1) / n_bars)
        k0 = min(int(f0 / bin_hz), len(mags) - 1)
        k1 = min(max(k0 + 1, int(f1 / bin_hz)), len(mags))
        weighted = sorted(
            (mags[k] * eq[k] for k in range(k0, k1)), reverse=True)
        if weighted:
            peak = sum(weighted[:_SPECTRUM_WIDE_BINS]) / min(
                _SPECTRUM_WIDE_BINS, len(weighted))
        else:
            peak = 0.0
        level = peak * _SPECTRUM_LEVEL_GAIN / _SPECTRUM_LEVEL_CLAMP
        levels.append(max(0.0, min(1.0, level)))
    return levels


def _fmt_time(seconds: float) -> str:
    """Format seconds as m:ss for the transport readout."""
    seconds = max(0.0, seconds)
    return f'{int(seconds) // 60:d}:{int(seconds) % 60:02d}'


def _unquote(text: str) -> str:
    """Drop one matching pair of surrounding quotes, if present."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        return text[1:-1]
    return text


@dataclass
class Track:
    """One queued audio file.

    Tags and an approximate duration are filled at queue time.  ``loaded``
    marks a completed decode, which replaces the duration with the exact
    sample count.
    """

    path: str
    duration: float = 0.0
    loaded: bool = False
    error: str = ''
    tags: TrackTags | None = None
    tags_read: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def display_title(self) -> str:
        title = self.tags.title if self.tags and self.tags.title else self.name
        return sanitize_text(title)


@dataclass
class _LoadJob:
    """Result slot for one background decode.

    A job object rather than shared result fields, so a decode that has
    been superseded cannot clobber the one currently being collected.
    """

    audio: array | None = None
    error: str | None = None
    done: bool = False


class FmTxView(View):
    """FM broadcast transmitter: playlist, now-playing panel, telemetry.

    ``:fm tx <path>`` queues a file rather than playing it.  j/k (or
    arrows) select, Enter loads and streams the selection, Space stops.
    Per playback: IDLE → LOADING → LOADED → STREAMING → DRAINING → DONE.
    The decoded audio of the playing track lives in ``_audio``; queued
    tracks carry tags and an approximate duration but no audio until one is
    played.
    """

    name = 'tx'
    hints = (':fm tx <dir|glob>  Enter:play/stop  Space:pause  ,/.:track  '
             '</>:seek  c:cont')
    mode = DaemonMode.FM

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self.style = DEFAULT_PALETTE
        self._clock = clock or time.monotonic
        self._window: StyledWindow | None = None
        self._band = BAND_FM
        self._freq_hz: int | None = None

        # Playlist
        self._playlist: list[Track] = []
        self._selected = 0
        self._playing: int | None = None
        self._offset = 0
        self._player_h = _TX_PLAYER_H
        self._playlist_visible = 0

        # Playback state
        self._state = 'idle'   # idle|loading|loaded|streaming|draining|done|paused
        self._audio: Sequence[float] = []
        self._audio_rate = 44100
        self._file_duration = 0.0
        self._sent = 0             # samples pushed so far
        self._resume_index = 0     # sample the paused stream resumes from
        # Mapping from the daemon's emitted DAC-sample counter to a track
        # sample: track position = _index_base + (emitted - _emit_base) *
        # audio_rate / dac_rate.  A fresh stream starts both at zero; a
        # keep-carrier seek rebases them past the queued audio.
        self._index_base = 0
        self._emit_base = 0
        self._stream_t0 = 0.0      # clock time when streaming began
        self._stats = None
        self._stats_at: float | None = None
        self._start_sync_until = 0.0
        self._retries = 0
        self._load_thread: threading.Thread | None = None
        self._load_job: _LoadJob | None = None
        self._status_msg = ''
        self._continuous = False   # auto-advance and wrap the playlist

        # RDS transmit fields (persisted as fm.tx.rds.*)
        self._rds_on = False
        self._rds_ps = ' ' * 8
        self._rds_rt = ' ' * 64
        self._rds_pi = 0
        self._rds_pty = 0
        self._rds_from_id3 = False

        # Spectrum bars: smoothed levels and falling peak caps.
        self._spec_levels: list[float] = []
        self._spec_peaks: list[float] = []
        self._spec_peak_anchor: list[float] = []
        self._spec_peak_age: list[float] = []
        self._spec_ts = 0.0

    # ─── Layout ─────────────────────────────────────────────────────────

    def layout(self, container: StyledWindow) -> list[Window]:
        self._player_h = _TX_PLAYER_H
        if container.height < self._player_h + 4:
            self._player_h = max(3, container.height - 3)
        self._window = StyledWindow(
            container.x, container.y, container.width, container.height,
            {'bg': self.style['bg'], 'fg': self.style['fg']})
        self._render()
        return [self._window]

    # ─── Lifecycle ──────────────────────────────────────────────────────

    def on_enter(self) -> None:
        if self.config:
            self._continuous = self.config.get_bool('fm.tx.continuous', False)
            self._load_rds_from_config()

    def _load_rds_from_config(self) -> None:
        cfg = self.config
        if not cfg:
            return
        self._rds_ps = cfg.get('fm.tx.rds.ps', ' ' * 8) or ' ' * 8
        self._rds_rt = cfg.get('fm.tx.rds.rt', ' ' * 64) or ' ' * 64
        self._rds_pi = cfg.get_int('fm.tx.rds.pi', 0)
        self._rds_pty = cfg.get_int('fm.tx.rds.pty', 0)
        self._rds_on = cfg.get_bool('fm.tx.rds.on', False) and self._rds_pi != 0
        self._rds_from_id3 = cfg.get_bool('fm.tx.rds.from_id3', False)

    def _probe_track(self, track: 'Track') -> None:
        """Fill tags and an approximate duration without decoding audio."""
        tags, duration = probe(track.path)
        track.tags = tags
        track.tags_read = True
        if duration is not None:
            track.duration = duration

    def _track_tags(self, track: 'Track') -> TrackTags | None:
        """Tags for *track*, read once and cached on the Track."""
        if not track.tags_read:
            track.tags = read_tags(track.path)
            track.tags_read = True
        return track.tags

    def _rds_config(self):
        """The current fields as the wire RdsTxConfig.

        With ``fm.tx.rds.from_id3`` on, the playing track's tags supply
        RadioText and PTY, falling back to the manual values when a field
        cannot be derived.  PS is always the configured station name.
        """
        from ..protocol import RdsTxConfig
        rt = self._rds_rt[:64]
        pty = self._rds_pty
        if self._rds_from_id3:
            track = self._playing_track()
            tags = self._track_tags(track) if track is not None else None
            if tags is not None:
                derived_rt, derived_pty = rds_from_tags(tags)
                if derived_rt is not None:
                    rt = derived_rt
                if derived_pty is not None:
                    pty = derived_pty
        return RdsTxConfig(enabled=self._rds_on, pi=self._rds_pi,
                           pty=pty, ps=self._rds_ps[:8], rt=rt)

    def rds_command(self, args: list[str],
                    daemon: 'Daemon') -> tuple[str | None, str]:
        """Handle ``:fm rds on|off|ps|rt|pi|pty|id3``."""
        if not args:
            return ('usage: :fm rds on|off | ps <text> | rt <text> | '
                    'pi <hex> | pty <n> | id3 on|off', '')
        op = args[0]
        if op == 'on':
            if self._rds_pi == 0:
                return ('set a PI first: :fm rds pi <hex>', '')
            self._rds_on = True
        elif op == 'off':
            self._rds_on = False
        elif op == 'id3':
            if len(args) < 2 or args[1] not in ('on', 'off'):
                return ('usage: :fm rds id3 on|off', '')
            self._rds_from_id3 = args[1] == 'on'
        elif op == 'ps':
            text = _unquote(' '.join(args[1:]))
            if len(text) > 8:
                return (f'ps too long (max 8): {text!r}', '')
            self._rds_ps = text.ljust(8)[:8]
        elif op == 'rt':
            text = _unquote(' '.join(args[1:]))
            if len(text) > 64:
                return (f'rt too long (max 64): {len(text)} chars', '')
            self._rds_rt = text.ljust(64)[:64]
        elif op == 'pi':
            if len(args) < 2:
                return ('usage: :fm rds pi <hex>', '')
            try:
                pi = int(args[1], 16)
            except ValueError:
                return (f'invalid pi: {args[1]}', '')
            if not 0 <= pi <= 0xFFFF:
                return (f'pi out of range: {args[1]}', '')
            self._rds_pi = pi
        elif op == 'pty':
            if len(args) < 2:
                return ('usage: :fm rds pty <n>', '')
            try:
                pty = int(args[1])
            except ValueError:
                return (f'invalid pty: {args[1]}', '')
            if not 0 <= pty <= 31:
                return (f'pty out of range: {args[1]}', '')
            self._rds_pty = pty
        else:
            return (f'unknown rds op: {op}', '')

        self._persist_rds()
        if self._state in ('streaming', 'draining'):
            daemon.set_rds(self._rds_config())
        self._render()
        return (None, '')

    def _persist_rds(self) -> None:
        if not self.config:
            return
        self.apply_config('fm.tx.rds.on',
                          'true' if self._rds_on else 'false')
        self.apply_config('fm.tx.rds.ps', self._rds_ps)
        self.apply_config('fm.tx.rds.rt', self._rds_rt)
        self.apply_config('fm.tx.rds.pi', str(self._rds_pi))
        self.apply_config('fm.tx.rds.pty', str(self._rds_pty))
        self.apply_config('fm.tx.rds.from_id3',
                          'true' if self._rds_from_id3 else 'false')

    def on_exit(self) -> None:
        self._stop_stream(release=True)

    def theme_changed(self, style: dict) -> None:
        self.style = style

    def config_changed(self, key: str, daemon: 'Daemon') -> None:
        if key == 'freq':
            freq = daemon.get_state().get('freq')
            if freq is not None:
                try:
                    self._freq_hz = int(float(freq))
                except (ValueError, TypeError):
                    pass

    def config_updated(self, key: str, value: str) -> None:
        if key == 'fm.tx.continuous':
            self._continuous = str(value).lower() in ('true', '1', 'yes', 'on')
        elif key.startswith('fm.tx.rds.'):
            self._load_rds_from_config()

    # ─── Tick (drives the streaming state machine) ──────────────────────

    def tick(self, dt: float) -> None:
        super().tick(dt)

        # Check if background load completed.  Starting the stream here
        # and returning keeps the first push on the next frame, so a load
        # never both starts and drains within one tick.  Completion is the
        # job's own flag, not thread liveness: keys run on the reader
        # thread, so a job can appear before its thread is started, and an
        # unstarted thread is not a finished decode.
        if self._state == 'loading':
            job = self._load_job
            if job is not None and job.done:
                self._finish_load(job)
                self._render()
                return

        # Push audio chunks when streaming
        if self._state == 'streaming':
            self._tick_stream()

        # Poll drain completion
        if self._state == 'draining':
            self._tick_drain()

        self._render()

    def _finish_load(self, job: '_LoadJob') -> None:
        """Collect a finished background decode and start playing."""
        if job is not self._load_job:
            return  # a skip replaced this job while the tick was running
        track = self._playing_track()
        if job.audio is not None:
            self._audio = job.audio
            self._file_duration = len(self._audio) / self._audio_rate
            if track is not None:
                track.duration = self._file_duration
                track.loaded = True
                track.error = ''
            self._state = 'loaded'
            self._start_streaming()
        else:
            if track is not None:
                track.error = job.error or 'load failed'
            self._audio = []
            self._state = 'idle'
            self._status_msg = job.error or 'load failed'

    def _tick_stream(self) -> None:
        """Push one chunk per tick if we're not too far ahead."""
        if not self.daemon:
            return
        total = len(self._audio)
        if self._sent >= total:
            # All audio pushed, signal end-of-stream
            self.daemon.fm_tx_stream_end()
            self._state = 'draining'
            self._status_msg = 'draining...'
            return

        elapsed = self._clock() - self._stream_t0
        ahead = self._sent / self._audio_rate - elapsed
        if ahead > _TX_LEAD_SEC:
            return  # too far ahead, wait for next tick

        chunk_n = max(1, int(self._audio_rate * _TX_CHUNK_SEC))
        end = min(self._sent + chunk_n, total)
        chunk = self._audio[self._sent:end]

        from ..client import PushResult
        result = self.daemon.fm_tx_stream_push(chunk)
        if result == PushResult.OK:
            self._sent = end
        elif result == PushResult.RETRY:
            self._retries += 1
            # Skip this tick, try again next frame
        else:
            self._state = 'idle'
            self._status_msg = f'push error at {self._sent}/{total}'

    def _tick_drain(self) -> None:
        """Poll tx_stats until the daemon finishes playout."""
        if not self.daemon:
            return
        # Check daemon state directly (arrives via RESP_STATUS push)
        state = self.daemon.get_state()
        if state.get('tx_busy') == '0':
            self._finish_playback()
            return
        # Also check tx_stats if available
        stats = self.daemon.tx_stats()
        if stats is not None and not stats.tx_active:
            self._finish_playback()

    def _finish_playback(self) -> None:
        """Track ended: advance when continuous, otherwise park at done."""
        if self._continuous and self._playing is not None and self._playlist:
            nxt = (self._playing + 1) % len(self._playlist)
            self._selected = nxt
            self._ensure_visible()
            self._sent = 0
            self._resume_index = 0
            self._audio = []
            self._state = 'idle'
            self._load_track(nxt)
            return
        self._state = 'done'
        self._status_msg = 'done'

    # ─── Commands / keybinds ────────────────────────────────────────────

    def command(self, args: list[str]) -> tuple[str | None, str]:
        """Handle :fm tx <path|dir|glob> — queue audio on the playlist.

        Joins all args as a single path so spaces work without quoting.
        A directory expands to its immediate audio files and a shell
        wildcard (``foo/*.mp3``) to its matches; non-audio entries are
        skipped.  Selection moves to the first queued track.
        """
        if not args:
            self._status_msg = 'usage: :fm tx <path|dir|glob>'
            return (self._status_msg, '')
        typed = ' '.join(args)
        pattern = os.path.expanduser(typed)
        if not has_glob(pattern) and not os.path.isdir(pattern):
            # A single explicit file: keep its specific queue-time error.
            reason = audio_file_error(pattern)
            if reason is not None:
                self._status_msg = reason
                self._render()
                return (reason, '')
            self._queue([pattern])
            return (None, '')

        paths = expand_paths(pattern)
        audio = [p for p in paths if audio_file_error(p) is None]
        if audio:
            self._queue(audio)
            return (None, '')
        if paths:
            self._status_msg = f'no audio files in {typed.rstrip(os.sep)}'
        else:
            self._status_msg = f'no files match: {typed}'
        self._render()
        return (self._status_msg, '')

    def _queue(self, paths: list[str]) -> None:
        """Append *paths* as tracks and select the first of the batch."""
        first = len(self._playlist)
        new = [Track(path) for path in paths]
        for track in new:
            self._probe_track(track)
        self._playlist.extend(new)
        self._selected = first
        self._ensure_visible()
        if len(paths) == 1:
            self._status_msg = f'queued {os.path.basename(paths[0])}'
        else:
            self._status_msg = f'queued {len(paths)} tracks'
        self._render()

    def word_completions(self) -> list[str]:
        return []

    def command_hints(self) -> list[tuple[str, str]]:
        return [('<path|dir|glob>', 'Queue audio for transmission')]

    def command_completions(self, text: str) -> list[str]:
        """Complete :fm tx arguments to audio files and drillable dirs."""
        return [
            path for path in complete_path(text)
            if path.endswith(os.sep)
            or audio_file_error(os.path.expanduser(path)) is None
        ]

    def _key_defs(self) -> list[tuple[str, str, Callable[[int], None]]]:
        last = lambda: len(self._playlist) - 1
        return [
            ('j', 'Select next track', lambda c: self._select_more(c)),
            ('k', 'Select previous track', lambda c: self._select_more(-c)),
            ('down', 'Select next track', lambda c: self._select_more(1)),
            ('up', 'Select previous track', lambda c: self._select_more(-1)),
            (Keyboard.ENTER, 'Play / stop / reset',
             lambda c: self._enter()),
            (' ', 'Pause / resume', lambda c: self._space()),
            (',', 'Previous track', lambda c: self._skip(-1)),
            ('.', 'Next track', lambda c: self._skip(1)),
            ('<', 'Seek back 5s', lambda c: self._seek(-c)),
            ('>', 'Seek forward 5s', lambda c: self._seek(c)),
            ('c', 'Toggle continuous', lambda c: self._toggle_continuous()),
            ('[', 'Fine tune down', lambda c: self._fine_tune(-c)),
            (']', 'Fine tune up', lambda c: self._fine_tune(c)),
            ('home', 'First track', lambda c: self._select_to(0)),
            ('end', 'Last track', lambda c: self._select_to(last())),
            ('g', 'First track', lambda c: self._select_to(0)),
            ('G', 'Last track', lambda c: self._select_to(last())),
            ('page_up', 'Page up',
             lambda c: self._select_more(-self._playlist_visible)),
            ('page_down', 'Page down',
             lambda c: self._select_more(self._playlist_visible)),
        ]

    # ─── Playlist navigation ────────────────────────────────────────────

    def _select_more(self, d: int) -> None:
        if not self._playlist or d == 0:
            return
        self._selected = max(0, min(len(self._playlist) - 1,
                                    self._selected + d))
        self._ensure_visible()
        self._render()

    def _select_to(self, idx: int) -> None:
        if not self._playlist:
            return
        self._selected = max(0, min(len(self._playlist) - 1, idx))
        self._ensure_visible()
        self._render()

    def _ensure_visible(self) -> None:
        """Scroll the playlist so the selected track is on screen."""
        vis = self._playlist_visible
        if vis <= 0 or not self._playlist:
            return
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + vis:
            self._offset = self._selected - vis + 1
        self._offset = max(0, min(self._offset,
                                  max(0, len(self._playlist) - vis)))

    def _playing_track(self) -> 'Track | None':
        if (self._playing is not None
                and 0 <= self._playing < len(self._playlist)):
            return self._playlist[self._playing]
        return None

    def _enter(self) -> None:
        """Enter: play the selection, stop the current track, or switch.

        Enter on the playing track stops it at 0:00; Enter again replays.
        Enter on a different track stops the current one and starts the
        selection in a single press.  A decode already in flight is left
        alone: stacking decodes wastes work and races their results.
        """
        if not self._playlist:
            self._status_msg = 'playlist empty — :fm tx <file>'
            return
        if self._state == 'loading':
            return
        if self._state in ('streaming', 'draining', 'paused'):
            switching = self._playing != self._selected
            self._halt()
            self._flash('tx_stop')
            self._sent = 0
            self._resume_index = 0
            self._reset_spectrum()
            if switching:
                self._audio = []
                self._state = 'idle'
                self._status_msg = ''
                self._load_track(self._selected)
            elif self._audio:
                self._state = 'loaded'
                self._status_msg = 'stopped'
            else:
                self._audio = []
                self._state = 'idle'
                self._status_msg = 'stopped'
            self._render()
            return
        self._flash('tx_play')
        if self._playing != self._selected or not self._audio:
            self._load_track(self._selected)
        else:
            self._start_streaming()

    def _telemetry(self, fresh: bool = False):
        """Cached TX telemetry, refreshed a few times a second."""
        now = self._clock()
        if (fresh or self._stats_at is None
                or now - self._stats_at >= _TX_STATS_INTERVAL):
            self._stats = self.daemon.tx_stats() if self.daemon else None
            self._stats_at = now
        return self._stats

    def _emitted_index(self, fresh: bool = False) -> int | None:
        """On-air track sample, or None until the DAC emits audio.

        ``emitted`` counts DAC samples since the stream was triggered;
        mapping it through the audio/DAC rate ratio recovers the track
        sample.  Seeks rebase both counters so the mapping survives a
        jump.
        """
        if self.daemon is None:
            return None
        in_sync = (
            not fresh
            and self._clock() < self._start_sync_until
            and (self._stats is None or not self._stats.tx_active
                 or self._stats.emitted <= self._emit_base))
        stats = self._telemetry(fresh or in_sync)
        if (stats is None or not stats.dac_rate
                or stats.emitted <= self._emit_base):
            return None
        emitted = stats.emitted
        if not fresh and stats.tx_active and self._stats_at is not None:
            elapsed = max(0.0, self._clock() - self._stats_at)
            projected = int(emitted + elapsed * stats.dac_rate)
            emitted = (min(projected, stats.fed)
                       if stats.fed >= emitted else projected)
        played = int((emitted - self._emit_base)
                     * self._audio_rate / stats.dac_rate)
        return max(0, min(len(self._audio), self._index_base + played))

    def _playhead(self) -> int:
        """Best estimate of the audio sample currently on air, in samples.

        Falls back to the pushed position only when the daemon has not
        reported any emission yet.
        """
        idx = self._emitted_index(fresh=True)
        if idx is not None:
            return idx
        return max(0, min(len(self._audio), self._sent))

    def _space(self) -> None:
        """Space: pause the live carrier, or resume from the saved spot."""
        if self._state in ('streaming', 'draining'):
            index = self._playhead()
            self._halt()
            self._flash('tx_pause')
            self._sent = index
            self._resume_index = index
            self._state = 'paused'
            self._status_msg = ''
        elif self._state == 'paused':
            self._flash('tx_play')
            self._start_streaming(from_index=self._resume_index)
        else:
            self._status_msg = 'nothing playing'

    def _skip(self, d: int) -> None:
        """Previous/next: select the adjacent track and play it from the top."""
        if not self._playlist:
            return
        new = max(0, min(len(self._playlist) - 1, self._selected + d))
        if new == self._selected or self._state == 'loading':
            return
        self._selected = new
        self._ensure_visible()
        self._halt()
        self._sent = 0
        self._resume_index = 0
        self._audio = []
        self._state = 'idle'
        self._load_track(self._selected)

    def _seek(self, steps: int) -> None:
        """Seek by *steps* x 5 s within the loaded track, keeping playback.

        While streaming this keeps the carrier up: the daemon flushes the
        audio it has queued but not yet modulated, and the feed jumps to
        the new offset.  Only the ~DDR-depth of already-modulated audio
        still plays before the jump takes effect.  The emitted counter is
        rebased so the playhead stays truthful.  A fresh start (paused,
        stopped) still goes through the stream start path.  Decoding is
        never retriggered; ``_audio`` holds the whole track.
        """
        if not self._audio:
            self._status_msg = 'load a track first'
            return
        self._flash('tx_seek_fwd' if steps > 0 else 'tx_seek_back')
        total = len(self._audio)
        if self._state in ('streaming', 'draining'):
            current = self._playhead()
        elif self._state == 'paused':
            current = self._resume_index
        else:
            current = self._sent
        target = max(0, min(total, current + int(steps * _TX_SEEK_SEC
                                                  * self._audio_rate)))
        if self._state in ('streaming', 'draining'):
            if target >= total:
                self._halt()
                self._sent = target
                self._resume_index = target
                self._state = 'done'
                self._status_msg = 'done'
            else:
                stats = self.daemon.tx_stats() if self.daemon else None
                self._stats = stats
                self._stats_at = None
                if self.daemon:
                    self.daemon.fm_tx_stream_flush()
                emitted = stats.emitted if stats is not None else 0
                depth = max(0, stats.depth) if stats is not None else 0
                self._emit_base = emitted + depth
                self._index_base = target
                self._sent = target
                self._stream_t0 = self._clock() - target / self._audio_rate
                self._start_sync_until = self._clock() + 2.0
                self._reset_spectrum()
                self._status_msg = (
                    f'seek {_fmt_time(target / self._audio_rate)}')
        elif self._state == 'paused':
            self._resume_index = target
            self._sent = target
            self._status_msg = ''
        elif self._state in ('loaded', 'done'):
            self._start_streaming(from_index=target)
        self._render()

    def _toggle_continuous(self) -> None:
        """Flip auto-advance and persist it as ``fm.tx.continuous``."""
        self._continuous = not self._continuous
        if self.config:
            self.apply_config(
                'fm.tx.continuous', 'true' if self._continuous else 'false')
        self._status_msg = f'continuous {"on" if self._continuous else "off"}'
        self._render()

    def _flash(self, name: str) -> None:
        """Pulse a transport button so a keypress is visible for a moment."""
        if self.animator is not None:
            self.animator.flash(name, count=1, duration=0.15)

    def _lit(self, name: str) -> bool:
        return bool(self.animator is not None and self.animator.get(name) > 0.5)

    def _halt(self) -> None:
        """Take the carrier down immediately, if it is up."""
        if self.daemon:
            try:
                self.daemon.tx_stop()
            except Exception:
                pass
        self._stats_at = None

    def _fine_tune(self, n: int) -> None:
        if not self.config or self._freq_hz is None:
            return
        new = self._freq_hz + n * self._band.fine_hz
        new = max(FREQ_MIN_HZ, min(FREQ_MAX_HZ, new))
        self._freq_hz = new
        self.apply_config('freq', str(new))

    # ─── Audio loading (background thread) ──────────────────────────────

    def _load_track(self, idx: int) -> None:
        """Begin decoding the playlist track at *idx* in a background thread."""
        track = self._playlist[idx]
        job = _LoadJob()
        self._playing = idx
        # Publish the unfinished job before the state, so a tick on the
        # main thread never mistakes the previous job for this load.
        self._load_job = job
        self._state = 'loading'
        self._status_msg = f'loading {track.name}...'
        self._audio = []
        self._file_duration = 0.0
        self._reset_spectrum()
        self._load_thread = threading.Thread(
            target=self._load_worker, args=(track.path, job), daemon=True)
        self._load_thread.start()

    def _load_worker(self, path: str, job: _LoadJob) -> None:
        """Run in background thread: decode + pre-emphasise audio.

        Any failure, including an unexpected exception, is reported through
        the job so the UI never sees a decode that simply vanishes.
        """
        try:
            raw = load_audio(path, self._audio_rate)
            if len(raw) < 2:
                job.error = 'audio too short'
                return
            audio, _peak, _scale = prepare_fm_audio(raw, self._audio_rate)
            job.audio = audio
        except AudioLoadError as exc:
            job.error = str(exc)
        except Exception as exc:      # noqa: BLE001 - surfaced to the user
            job.error = f'decode failed: {exc}'
        finally:
            job.done = True

    # ─── Stream control ─────────────────────────────────────────────────

    def _start_streaming(self, from_index: int = 0) -> None:
        """Begin FM TX streaming from *from_index* samples into the track.

        A resume after pause passes the saved playhead, so the stream picks
        up where it left off rather than restarting the track.
        """
        if self._state not in ('loaded', 'done', 'paused') or not self._audio:
            return
        if not self.daemon:
            self._status_msg = 'no daemon'
            return

        total = len(self._audio)
        from_index = max(0, min(from_index, total))
        first_n = min(self._audio_rate, total - from_index)  # ~1s prefill
        if first_n <= 0:
            self._state = 'done'
            self._status_msg = 'done'
            return

        ok = self.daemon.fm_tx_stream_start(
            self._audio[from_index:from_index + first_n],
            audio_rate=self._audio_rate,
            rds=self._rds_config())
        if not ok:
            self._status_msg = 'stream start rejected'
            return

        self._sent = from_index + first_n
        self._index_base = from_index
        self._emit_base = 0
        self._retries = 0
        # Backdate the clock by the resumed offset so the displayed time
        # is the track position, not the time since this stream started.
        self._stream_t0 = self._clock() - from_index / self._audio_rate
        self._stats_at = None
        self._start_sync_until = self._clock() + 2.0
        self._state = 'streaming'
        # The accented ▶ in the transport is the playing indicator; no
        # redundant 'streaming...' message.
        self._status_msg = ''
        self._reset_spectrum()

    def _stop_stream(self, release: bool = False) -> None:
        """Stop streaming; Enter replays the track from the top.

        CMD_TX_STOP stops sample generation and asks the daemon to shut the
        DMA down; it may take a moment to drain what the engine already
        holds.  END alone would only ask the daemon to drain its ring, which
        leaves the carrier up for as long as the buffered audio lasts — and
        forever if the daemon never sees the END.

        release=True (leaving the view, or switching tracks) drops the
        decoded audio; a plain stop keeps it.  The playlist is untouched
        either way.
        """
        if self._state in ('streaming', 'draining'):
            self._halt()
        self._sent = 0
        self._resume_index = 0
        self._reset_spectrum()
        if release or not self._audio:
            self._audio = []
            self._state = 'idle'
            self._status_msg = ''
        else:
            self._state = 'loaded'
            self._status_msg = 'stopped — Enter to replay'

    # ─── Rendering ──────────────────────────────────────────────────────

    def _render(self) -> None:
        w = self._window
        if w is None:
            return
        height = w.height
        playlist_h = height - self._player_h - 1
        if playlist_h < 3:
            playlist_h = 3
            self._player_h = max(3, height - playlist_h - 1)
        self._playlist_visible = max(0, playlist_h - 3)
        lines = (self._player_lines(w.width)
                 + self._playlist_lines(w.width, playlist_h)
                 + [self._status_line(w.width)])
        w.update_buffer(_pad(lines, w.width, height))

    # -- box drawing -----------------------------------------------------

    def _box_top(self, title: str, width: int) -> str:
        lead = f'\u250c\u2500 {title} '
        return Text(lead + '\u2500' * (width - len(lead) - 1)
                    + '\u2510').style({'fg': self.style['dim']})

    def _box_bottom(self, width: int) -> str:
        return Text('\u2514' + '\u2500' * (width - 2)
                    + '\u2518').style({'fg': self.style['dim']})

    def _box_row(self, styled: str, plain: str, width: int) -> str:
        pad = ' ' * max(0, width - 2 - display_width(plain))
        return (Text('\u2502').style({'fg': self.style['dim']})
                + styled + pad
                + Text('\u2502').style({'fg': self.style['dim']}))

    # -- now-playing panel ----------------------------------------------

    def _spectrum_samples(self) -> Sequence[float] | None:
        """The on-air audio window, or None before anything is emitted.

        Centre the analysis window on the playhead.  A Hann window's
        information is centred, so ending the window at the playhead makes
        the bars lag by half a window (~12 ms at 44.1 kHz).  The track is
        already decoded, so the window may include a little audio that has
        not been emitted yet.
        """
        idx = self._emitted_index()
        if idx is None or idx < 8:
            return None
        half = _SPECTRUM_FFT_MAX // 2
        start = max(0, idx - half)
        end = min(len(self._audio), idx + half)
        return self._audio[start:end]

    def _bar_color(self, py: int, pixels: int) -> tuple[int, int, int]:
        """Colour for an absolute chart row, top (fg) to bottom (accent)."""
        t = py / max(1, pixels - 1)
        top = self.style['fg']
        bottom = self.style['accent']
        return tuple(int(top[i] + (bottom[i] - top[i]) * t)
                     for i in range(3))

    def _spectrum_rows(self, width: int) -> list[str]:
        """The playing bar chart, half-block drawn across the panel."""
        cw = max(1, width)
        blank = ' ' * cw
        if self._state not in ('streaming', 'draining'):
            return [blank] * _SPECTRUM_ROWS
        samples = self._spectrum_samples()
        if not samples:
            return [blank] * _SPECTRUM_ROWS
        bars = max(4, min(32, (cw + 1) // 3))
        levels = self._update_spectrum(bars, samples)
        peaks = self._spec_peaks
        pixels = _SPECTRUM_ROWS * 2
        canvas = PixelCanvas(cw, pixels, self.style['bg'])
        cap = self.style['warn']
        for b in range(bars):
            h = int(round(levels[b] * (pixels - 1)))
            x0 = b * 3
            for dx in (0, 1):
                x = x0 + dx
                if x >= cw:
                    continue
                for py in range(pixels - h, pixels):
                    canvas.set(x, py, self._bar_color(py, pixels))
                if peaks[b] > 0.0:
                    cap_h = int(round(peaks[b] * (pixels - 1)))
                    if cap_h >= 1:
                        canvas.set(x, max(0, pixels - cap_h - 1), cap)
        return canvas.draw().split('\n')

    def _reset_spectrum(self) -> None:
        self._spec_levels = []
        self._spec_peaks = []
        self._spec_peak_anchor = []
        self._spec_peak_age = []
        self._spec_ts = 0.0

    def _update_spectrum(self, n_bars: int,
                         samples: Sequence[float]) -> list[float]:
        """Refresh the bar levels and peak caps for this frame."""
        raw = _spectrum_levels(samples, self._audio_rate, n_bars)
        now = self._clock()
        if len(self._spec_levels) != n_bars:
            self._reset_spectrum()
            self._spec_levels = list(raw)
            self._spec_peaks = list(raw)
            self._spec_peak_anchor = list(raw)
            self._spec_peak_age = [0.0] * n_bars
            self._spec_ts = now
            return self._spec_levels
        dt = 1.0 / 60.0
        if self._spec_ts:
            dt = min(0.5, max(0.0, now - self._spec_ts))
        self._spec_ts = now
        return self._advance_spectrum(raw, dt)

    def _advance_spectrum(self, raw: list[float],
                          dt: float) -> list[float]:
        """Fall bars, then hang and release each peak cap."""
        n = len(raw)
        if len(self._spec_levels) != n:
            self._reset_spectrum()
            self._spec_levels = list(raw)
            self._spec_peaks = list(raw)
            self._spec_peak_anchor = list(raw)
            self._spec_peak_age = [0.0] * n
            return self._spec_levels
        fall = _SPECTRUM_FALL_PER_S * dt
        for i in range(n):
            level = max(raw[i], self._spec_levels[i] - fall)
            self._spec_levels[i] = level
            if level >= self._spec_peaks[i]:
                self._spec_peaks[i] = level
                self._spec_peak_anchor[i] = level
                self._spec_peak_age[i] = 0.0
                continue
            age = self._spec_peak_age[i] + dt
            self._spec_peak_age[i] = age
            growth = _SPECTRUM_PEAK_ACCEL ** (age * 60.0)
            drop = (_SPECTRUM_PEAK_DROP * (growth - 1.0)
                    / (_SPECTRUM_PEAK_ACCEL - 1.0))
            cap = self._spec_peak_anchor[i] - drop
            if cap <= level:
                self._spec_peaks[i] = level
                self._spec_peak_anchor[i] = level
                self._spec_peak_age[i] = 0.0
            else:
                self._spec_peaks[i] = cap
        return self._spec_levels

    def _rds_line(self) -> str:
        """One line describing what the transmitter is broadcasting.

        Reads the configured fields, not the RX decoder — this is what we
        send, which is what the receiver should show.
        """
        if not self._rds_on:
            return ' RDS \u25cb  off'
        rds = self._rds_config()
        ps = sanitize_text(rds.ps.strip()) or '------'
        rt = sanitize_text(rds.rt.rstrip()) or '(none)'
        pty = pty_text(rds.pty) or '--'
        return f' RDS \u25cf  PS: {ps}   RT: {rt}   PTY: {pty}'

    def _player_lines(self, width: int) -> list[str]:
        dim = self.style['dim']
        fg = self.style['fg']
        accent = self.style['accent']
        track = self._playing_track()
        if track is None and self._playlist:
            track = self._playlist[self._selected]
        name = track.display_title if track else '(no track queued)'
        artist = (sanitize_text(track.tags.artist.strip())
                  if track and track.tags else '')
        title = f'{artist} - {name}' if artist else name
        folder = sanitize_text(os.path.dirname(track.path)) if track else ''
        dur = track.duration if track else 0.0

        playing = self._state in ('streaming', 'draining')
        paused = self._state == 'paused'
        if playing:
            elapsed = self._clock() - self._stream_t0 if self._stream_t0 else 0.0
        elif paused:
            elapsed = self._sent / self._audio_rate if self._audio_rate else 0.0
        else:
            elapsed = 0.0
        time_str = f'{_fmt_time(elapsed)} / {_fmt_time(dur)}'

        def btn(text: str, on: bool) -> str:
            return Text(text).style({'fg': accent if on else fg, 'bold': on})

        # rewind | play | pause | stop | forward     continuous
        transport = (
            Text(' ').style({'fg': dim})
            + btn('[\u25c0\u25c0]', self._lit('tx_seek_back'))
            + Text(' ').style({'fg': dim})
            + btn('[\u25b6]', playing or self._lit('tx_play'))
            + Text(' ').style({'fg': dim})
            + btn('[\u2016]', paused or self._lit('tx_pause'))
            + Text(' ').style({'fg': dim})
            + btn('[\u25a0]', (not playing and not paused)
                  or self._lit('tx_stop'))
            + Text(' ').style({'fg': dim})
            + btn('[\u25b6\u25b6]', self._lit('tx_seek_fwd'))
            + Text('      ').style({'fg': dim})
            + btn('[\u21bb cont]', self._continuous))
        transport_plain = (' [\u25c0\u25c0] [\u25b6] [\u2016] [\u25a0] '
                           '[\u25b6\u25b6]      [\u21bb cont]')

        # Seek bar: elapsed position; the time readout lives here now.
        bar_w = max(10, width - 2 - len(time_str) - 5)
        frac = (elapsed / dur) if dur > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        pos = int(frac * (bar_w - 1))
        filled = '\u2501' * pos
        empty = '\u2500' * (bar_w - 1 - pos)
        seek = (Text(' [').style({'fg': dim})
                + Text(filled).style({'fg': accent})
                + Text('\u25cf').style({'fg': accent, 'bold': True})
                + Text(empty).style({'fg': dim})
                + Text(']').style({'fg': dim})
                + Text(f'  {time_str}').style({'fg': fg}))
        seek_plain = f' [{filled}\u25cf{empty}]  {time_str}'

        wf = self._spectrum_rows(max(1, width - 6))
        rds_plain = self._rds_line()[:max(0, width - 2)]
        rds_color = fg if self._rds_on else dim
        title_plain = f' {title}'
        subtitle = folder
        if self._status_msg:
            status = sanitize_text(self._status_msg)
            subtitle = f'{folder}   {status}' if folder else status
        path_plain = f'   {subtitle}'

        wf_rows = [
            self._box_row(Text('  ' + r).style({'fg': accent}),
                          '  ' + r, width)
            for r in wf
        ]

        rows = [
            self._box_top('playing', width),
            self._box_row(Text(title_plain).style({'fg': fg, 'bold': True}),
                          title_plain, width),
            self._box_row(Text(path_plain).style({'fg': dim}),
                          path_plain, width),
            self._box_row(transport, transport_plain, width),
            self._box_row(seek, seek_plain, width),
            *wf_rows,
            self._box_row(Text(rds_plain).style({'fg': rds_color}),
                          rds_plain, width),
            self._box_bottom(width),
        ]
        if len(rows) <= self._player_h:
            return rows
        return rows[:max(1, self._player_h - 1)] + [rows[-1]]

    # -- playlist panel --------------------------------------------------

    def _playlist_lines(self, width: int, height: int) -> list[str]:
        dim = self.style['dim']
        fg = self.style['fg']
        accent = self.style['accent']
        bg = self.style['bg']
        n = len(self._playlist)
        tw = max(1, width - 15)
        rows = [
            self._box_top(f'playlist ({n})', width),
        ]
        vis = max(0, height - 2)
        for i in range(self._offset, min(n, self._offset + vis)):
            t = self._playlist[i]
            mark = '\u25b6' if i == self._playing else ' '
            title = sanitize_text(t.error or t.display_title)
            length = _fmt_time(t.duration) if t.duration > 0 else '--:--'
            plain = f' {mark} {i + 1:<2} {title:<{tw}}{length:>6} '
            if i == self._selected:
                styled = Text(plain).style({'bg': accent, 'fg': bg,
                                            'bold': True})
            else:
                prefix = f' {mark} {i + 1:<2} '
                body = f'{title:<{tw}}{length:>6} '
                styled = (Text(prefix).style(
                              {'fg': accent if i == self._playing else fg,
                               'bold': i == self._playing})
                          + Text(body).style({'fg': dim if t.error else fg}))
            rows.append(self._box_row(styled, plain, width))
        blank = ' ' * (width - 2)
        while len(rows) < height - 1:
            rows.append(self._box_row(Text(blank).style({'fg': dim}),
                                      blank, width))
        rows.append(self._box_bottom(width))
        return rows[:height]

    # -- telemetry strip -------------------------------------------------

    def _status_line(self, width: int) -> str:
        dim = self.style['dim']
        fg = self.style['fg']
        stats = None
        if self._state in ('streaming', 'draining') and self.daemon:
            stats = self._telemetry()
        if stats is not None:
            fields = [
                (' depth ', f'{stats.depth_ms:>4.0f}ms', 6),
                (' ring ', f'{stats.ring_fill_ms:>4.0f}ms', 6),
                (' rate ', f'{stats.rate_frac * 100:>6.2f}%', 7),
                (' underruns ', f'{stats.underruns}', 5),
                (' fed ', f'{stats.fed / 1e6:>5.1f}M', 6),
                (' emitted ', f'{stats.emitted / 1e6:>5.1f}M', 6),
            ]
            value_color = fg
        else:
            fields = [
                (' depth ', '--', 6),
                (' ring ', '--', 6),
                (' rate ', '--', 7),
                (' underruns ', '--', 5),
                (' fed ', '--', 6),
                (' emitted ', '--', 6),
            ]
            value_color = dim

        def field(label: str, value: str, value_w: int) -> tuple[str, str]:
            cell = value[:value_w].rjust(value_w)
            styled = (Text(label).style({'fg': dim})
                      + Text(cell).style({'fg': value_color}))
            return styled, label + cell

        left = right = ''
        lp = rp = ''
        for i, (label, value, value_w) in enumerate(fields):
            styled, plain = field(label, value, value_w)
            if i < 4:
                left += styled
                lp += plain
            else:
                right += styled
                rp += plain
        gap = ' ' * max(0, width - len(lp) - len(rp))
        return left + gap + right


class FmApp(App):
    """Broadcast FM receiver/transmitter app."""

    name = 'fm'
    description = 'fm transmit and receive'
    default_view = 'rx'
    command_descriptions = {'rds': 'Control RDS (on|off|id3|ps|rt|pi|pty)'}
    command_usage = {'rds': '<operation> [value]'}

    @classmethod
    def config_keys(cls) -> dict[str, KeySpec]:
        return {
            'fm.tx.continuous': KeySpec('bool', default=False),
            'fm.tx.rds.on':     KeySpec('bool', default=False),
            'fm.tx.rds.ps':     KeySpec('str', default=' ' * 8),
            'fm.tx.rds.rt':     KeySpec('str', default=' ' * 64),
            'fm.tx.rds.pi':     KeySpec('int', default=0, min=0, max=0xFFFF),
            'fm.tx.rds.pty':    KeySpec('int', default=0, min=0, max=31),
            'fm.tx.rds.from_id3': KeySpec('bool', default=False),
        }

    _RDS_OPS = ('on', 'off', 'id3', 'ps', 'rt', 'pi', 'pty')

    def __init__(self) -> None:
        super().__init__()
        self.views = {'rx': FmRxView(), 'tx': FmTxView()}
        self.commands['rds'] = self._rds_command

    def command_completions(self, name: str, text: str) -> list[str]:
        if name != 'rds':
            return []
        tokens = text.split()
        if not tokens:
            return list(self._RDS_OPS)
        if len(tokens) == 1 and not text.endswith(' '):
            return [op for op in self._RDS_OPS if op.startswith(tokens[0])]
        if tokens[0] == 'id3':
            return [f'id3 {value}' for value in ('on', 'off')
                    if f'id3 {value}'.startswith(text)]
        return []

    def _rds_command(self, args: list[str],
                     daemon: 'Daemon') -> tuple[str | None, str]:
        return self.views['tx'].rds_command(args, daemon)
