"""Status bar layout and animation-driven rendering."""
from __future__ import annotations

import time

from .animate import Animator
from .fmt import format_freq
from .pytui import StyledWindow, Text
from ..channels import FREQ_TO_CHANNEL


class StatusBar:
    """Renders the 3-pane status bar (left: daemon+tx glyphs, middle: mode, right: radio).

    Depends only on an Animator and a style dict. Does NOT import Shell.
    """

    def __init__(self, animator: Animator, style: dict,
                 clock=None) -> None:
        self._animator = animator
        self.style = style
        # Injectable wall clock (returns time.struct_time) so headless
        # snapshots do not leak the real time of day into the render.
        self._clock = clock or time.localtime

    @staticmethod
    def _lerp_color(a: tuple, b: tuple, t: float) -> tuple:
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    # ─── Full update (writes to windows) ─────────────────────────────────

    def update(self, *, left: StyledWindow, middle: StyledWindow,
               right: StyledWindow, state: dict,
               busy: bool, connected: bool,
               app_view_text: str) -> None:
        """Render all 3 status bar panes into the given windows."""
        self._update_left(left, busy, connected, state)
        self._update_middle(middle, app_view_text)
        self._update_right(right, state)

    def _update_left(self, win: StyledWindow, busy: bool, connected: bool,
                     state: dict) -> None:
        """Draw the daemon and TX glyphs.

        Connection state is ambient — carried by the daemon glyph's colour
        and animation rather than text.  Only two facts matter: something
        is happening (busy -> pulse), or nothing is (connected -> throb,
        otherwise red).  See docs/tui.md.
        """
        style = self.style
        base_color = style['fg'] if (connected or busy) else style['warn']

        # Only lerp toward the base colour while an animation is running;
        # otherwise the glyph sits at full base (steady red when offline).
        t = self._animator.get('daemon')
        if t > 0.01 or self._animator.running('daemon'):
            daemon_color = self._lerp_color(style['dim'], base_color, t)
        else:
            daemon_color = base_color

        tx_color = style['warn'] if state.get('tx_busy', '0') == '1' else style['dim']

        left_content = (
            Text(f' {style["glyph_daemon"]}').style({'fg': daemon_color})
            + Text(f' {style["glyph_tx"]}').style({'fg': tx_color}))
        win.update_buffer([win.justify_line(left_content, 'left')])

    def _update_middle(self, win: StyledWindow, app_view_text: str) -> None:
        fpga_t = self._animator.get('fpga_active')
        if fpga_t > 0.01 or self._animator.running('fpga_active'):
            color = self._lerp_color(self.style['fg'], self.style['accent'], fpga_t)
        else:
            color = self.style['fg']
        # Center the plain text first, then style the entire padded line.
        # This avoids an ANSI reset mid-line which some terminals mishandle.
        padded = win.justify_line(app_view_text, 'center')
        styled = Text(padded).style({'fg': color})
        win.update_buffer([styled])

    def _update_right(self, win: StyledWindow, state: dict) -> None:
        try:
            freq_hz = int(float(state.get('freq', '0')))
        except (ValueError, TypeError):
            freq_hz = 0

        # WiFi channels are keyed by integer MHz and the LO rarely lands on
        # an exact MHz, so snap to the nearest MHz to find the channel.
        snap_mhz = round(freq_hz / 1_000_000)

        try:
            bw_hz = int(float(state.get('bw', '0')))
        except (ValueError, TypeError):
            bw_hz = 0

        # Only quantize the display when the snap lands on a channel, so the
        # label and the number agree; otherwise show the raw frequency, which
        # preserves FM broadcast's fractional MHz.
        ch = FREQ_TO_CHANNEL.get(snap_mhz)
        ch_text = f' ({ch})' if ch else ''

        agc_mode = state.get('agc_mode', 'manual')
        gain_str = state.get('gain', '0')
        if agc_mode in ('fast', 'slow'):
            gain_text = agc_mode
        else:
            try:
                gain_text = f'{int(float(gain_str))}dB'
            except (ValueError, TypeError):
                gain_text = f'{gain_str}dB'

        # Per-field animation colors
        freq_t = self._animator.get('status_freq')
        bw_t = self._animator.get('status_bw')
        gain_t = self._animator.get('status_gain')
        heat = self._animator.get('gain_heat')

        def _field_color(av: float, name: str) -> tuple:
            if av > 0.01 or self._animator.running(name):
                return self._lerp_color(self.style['dim'], self.style['fg'], av)
            return self.style['fg']

        freq_color = _field_color(freq_t, 'status_freq')
        bw_color = _field_color(bw_t, 'status_bw')
        if heat > 0.01 or self._animator.running('gain_heat'):
            gain_color = self._lerp_color(self.style['fg'],
                                          self.style['warn'], heat)
        else:
            gain_color = _field_color(gain_t, 'status_gain')

        now = self._clock()
        time_str = f'{now.tm_hour:02d}:{now.tm_min:02d}'

        if ch is None:
            freq_base = format_freq(freq_hz)
        else:
            freq_base = format_freq(snap_mhz * 1_000_000)
        freq_plain = freq_base + ch_text
        bw_plain = format_freq(bw_hz)

        styled = {
            'freq': Text(freq_plain).style({'fg': freq_color}),
            'bw': Text(bw_plain).style({'fg': bw_color}),
            'gain': Text(gain_text).style({'fg': gain_color}),
            'time': time_str,
        }
        plain = {'freq': freq_plain, 'bw': bw_plain,
                 'gain': gain_text, 'time': time_str}

        # A trailing space keeps the text off the right edge of the pane.
        trailing = ' '

        # Fields in display order. When the pane is too narrow they drop out
        # clock first, then the optional channel suffix, then bandwidth, then
        # gain. The clock is already on the desktop and the channel is a
        # convenience; frequency, bandwidth and gain are the minimum state.
        shown = ['freq', 'bw', 'gain', 'time']

        def budget() -> int:
            return len(' - '.join(plain[n] for n in shown)) + len(trailing)

        if budget() > win.width:
            shown.remove('time')

        if budget() > win.width and ch is not None:
            plain['freq'] = freq_base
            styled['freq'] = Text(freq_base).style({'fg': freq_color})

        for name in ('bw', 'gain'):
            if budget() <= win.width:
                break
            shown.remove(name)

        right_content = ' - '.join(styled[n] for n in shown) + trailing
        win.update_buffer([win.justify_line(right_content, 'right')])

    # ─── REC Status Bar ──────────────────────────────────────────────────

    @staticmethod
    def update_rec(*, left: StyledWindow, right: StyledWindow,
                   style: dict, label: str = '', frames: int = 0) -> None:
        """Render the recording status bar into two panes."""
        left_text = f' REC: {label}' if label else ' REC'
        left_content = Text(left_text).style({'fg': style['fg']})
        left.update_buffer([left.justify_line(left_content, 'left')])

        right_text = f'{frames} frames  '
        right_content = Text(right_text).style({'fg': style['fg']})
        right.update_buffer([right.justify_line(right_content, 'right')])
