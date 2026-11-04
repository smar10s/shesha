"""Tests for the extracted StatusBar module.

These exercise the live render path, so a formatting regression there
cannot hide behind a parallel test-only implementation.
"""
from shesha.tui.status_bar import StatusBar
from shesha.tui.animate import Animator
from shesha.tui.pytui import StyledWindow, Text
from shesha.tui.theme import DEFAULT_PALETTE


# Wide enough that the width budget never sheds a field. These tests are
# about formatting; shedding is covered in test_shell_resize.py.
WIDE = 60


def render_right(state: dict) -> str:
    """Render the right pane and read back what a user would see."""
    sb = StatusBar(animator=Animator(), style=DEFAULT_PALETTE)
    win = StyledWindow(0, 0, WIDE, 1)
    sb._update_right(win, state)
    return Text(win.buffer[0]).strip_ansi()


def test_status_bar_renders_freq():
    """StatusBar renders frequency from daemon state in Hz."""
    state = {'freq': '5180000000', 'bw': '20000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '5.18 GHz' in lines


def test_status_bar_renders_agc_mode():
    """StatusBar shows AGC mode name instead of dB when AGC active."""
    state = {'freq': '2457000000', 'bw': '20000000', 'gain': 'fast', 'agc_mode': 'fast'}
    lines = render_right(state)
    assert 'fast' in lines


def test_status_bar_renders_channel():
    """StatusBar shows channel number for known frequencies."""
    state = {'freq': '2412000000', 'bw': '20000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    # 2412 MHz is channel 1
    assert '(1)' in lines


def test_status_bar_renders_gain_db():
    """StatusBar shows gain in dB for manual mode."""
    state = {'freq': '5180000000', 'bw': '40000000', 'gain': '72', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '72dB' in lines


def test_status_bar_renders_bw():
    """StatusBar shows bandwidth."""
    state = {'freq': '5180000000', 'bw': '40000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '40 MHz' in lines


def test_status_bar_quantizes_display_to_mhz():
    """Display and channel label must agree.

    The AD9361 LO rarely lands exactly on the requested Hz, so the
    channel lookup snaps to the nearest MHz.  When that snap lands on a
    channel the display is quantized to match, so a freq 1 Hz below a
    boundary can't display '5.181' while labelling ch36 (the '5.181 as
    ch36' bug).
    """
    # 5180.999999 GHz rounds to 5181 MHz -> 5.181, and is NOT ch36
    state = {'freq': '5180999999', 'bw': '20000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '5.181 GHz' in lines
    assert '(36)' not in lines

    # 1 Hz above the center rounds back to 5180 -> ch36 with matching text
    state = {'freq': '5180000001', 'bw': '20000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '5.18 GHz' in lines
    assert '(36)' in lines


def test_status_bar_off_channel_freq_has_no_channel_label():
    """Tuning 1 MHz off center shows the raw MHz and no channel label."""
    state = {'freq': '5181000000', 'bw': '20000000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '5.181 GHz' in lines
    assert '(36)' not in lines


def test_status_bar_fm_broadcast_keeps_fractional_mhz():
    """FM broadcast has no channel table, so the raw frequency is shown."""
    state = {'freq': '100100000', 'bw': '200000', 'gain': '50', 'agc_mode': 'manual'}
    lines = render_right(state)
    assert '100.1 MHz' in lines


def _render_right_with(animator: Animator, state: dict) -> str:
    sb = StatusBar(animator=animator, style=DEFAULT_PALETTE)
    win = StyledWindow(0, 0, WIDE, 1)
    sb._update_right(win, state)
    return win.buffer[0]


def _fg_code(color: tuple) -> str:
    return f'\x1b[38;2;{color[0]};{color[1]};{color[2]}m'


_GAIN_STATE = {'freq': '5180000000', 'bw': '20000000',
               'gain': '72', 'agc_mode': 'manual'}


def test_gain_heat_starts_at_fg():
    raw = _render_right_with(Animator(), _GAIN_STATE)
    assert _fg_code(DEFAULT_PALETTE['fg']) in raw
    assert _fg_code(DEFAULT_PALETTE['warn']) not in raw


def test_gain_heat_halfway_lerps_fg_to_warn():
    a = Animator()
    a.to('gain_heat', 1.0, duration=1.0, easing='linear')
    a.tick(0.5)
    raw = _render_right_with(a, _GAIN_STATE)
    half = StatusBar._lerp_color(DEFAULT_PALETTE['fg'], DEFAULT_PALETTE['warn'], 0.5)
    assert _fg_code(half) in raw


def test_gain_heat_full_is_warn():
    a = Animator()
    a.to('gain_heat', 1.0, duration=1.0, easing='linear')
    a.tick(1.0)
    raw = _render_right_with(a, _GAIN_STATE)
    assert _fg_code(DEFAULT_PALETTE['warn']) in raw


def test_heat_colour_wins_over_the_send_pulse():
    a = Animator()
    a.to('gain_heat', 1.0, duration=1.0, easing='linear')
    a.tick(1.0)
    a.pulse('status_gain', frequency=1.0, min_val=0.4, max_val=1.0,
            phase_offset=0.25)
    raw = _render_right_with(a, _GAIN_STATE)
    assert _fg_code(DEFAULT_PALETTE['warn']) in raw


def test_send_pulse_still_colours_gain_without_heat():
    a = Animator()
    a.pulse('status_gain', frequency=1.0, min_val=0.4, max_val=1.0,
            phase_offset=0.25)
    raw = _render_right_with(a, _GAIN_STATE)
    assert _fg_code(DEFAULT_PALETTE['warn']) not in raw


def _render_left(state: dict, *, busy: bool = False,
                 connected: bool = True) -> str:
    sb = StatusBar(animator=Animator(), style=DEFAULT_PALETTE)
    win = StyledWindow(0, 0, WIDE, 1)
    sb._update_left(win, busy, connected, state)
    return win.buffer[0]


def test_tx_glyph_is_warn_while_transmitting():
    raw = _render_left({'tx_busy': '1'}, busy=True)
    assert _fg_code(DEFAULT_PALETTE['warn']) in raw


def test_tx_glyph_is_dim_when_idle():
    raw = _render_left({'tx_busy': '0'})
    assert _fg_code(DEFAULT_PALETTE['dim']) in raw
    assert _fg_code(DEFAULT_PALETTE['warn']) not in raw
