"""The shell must lay out cleanly at every supported terminal size.

A resize below the supported floor previously crashed the render.
"""
import json
import os
import re

import pytest

import shesha.tui.pytui as pytui
from shesha.channels import CHANNEL_TO_FREQ
from shesha.tui.animate import Animator
from shesha.tui.config import Config
from shesha.tui.dummy import DummyDaemon
from shesha.tui.pytui import StyledWindow, Text
from shesha.tui.shell import Shell
from shesha.tui.status import StatusApp
from shesha.tui.status_bar import StatusBar
from shesha.tui.theme import DEFAULT_PALETTE
from shesha.tui.viz import VizApp
from shesha.tui.fm import FmApp


# 80x24 is the supported floor. Anything above it must also work.
SIZES = [(80, 24), (90, 30), (100, 30), (110, 30), (120, 30), (200, 50)]


@pytest.fixture
def shell(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({}))
    config = Config(str(path))
    daemon = DummyDaemon(config)
    return Shell(apps=[StatusApp(), VizApp(), FmApp()],
                 config=config, daemon=daemon)


@pytest.mark.parametrize('cols,lines', SIZES)
def test_relayout_at_size(shell, monkeypatch, cols, lines):
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((cols, lines)))
    shell._relayout()


TINY = [(79, 24), (80, 23), (40, 20), (10, 4), (10, 2), (1, 1)]


@pytest.mark.parametrize('cols,lines', TINY)
def test_relayout_below_the_floor_does_not_raise(shell, monkeypatch,
                                                cols, lines):
    """Undersized terminals degrade to a message instead of killing the TUI."""
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((cols, lines)))
    shell._relayout()


def test_below_floor_message_appears_and_clears(shell, monkeypatch):
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((79, 24)))
    shell._relayout()
    body = '\n'.join(Text(line).strip_ansi() for line in shell._body.buffer)
    assert 'too small' in body

    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((80, 24)))
    shell._relayout()
    body = '\n'.join(Text(line).strip_ansi() for line in shell._body.buffer)
    assert 'too small' not in body


@pytest.mark.parametrize('cols,lines', SIZES)
def test_theme_switch_at_size(shell, monkeypatch, cols, lines):
    """Switching theme triggers a relayout, so it must survive small sizes."""
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((cols, lines)))
    shell._apply_theme('tokyonight')
    shell._apply_theme('cyberpunk')


def test_terminal_size_queries_the_render_tty_not_stdout(monkeypatch):
    """fd 1 points at the log during a run, so size must come from the tty.

    Using os.get_terminal_size()'s default fd would fail and silently pin
    the render to 80x24.
    """
    term = pytui.Terminal()

    class FakeStream:
        def fileno(self):
            return 42

    monkeypatch.setattr(pytui, '_render_stream', FakeStream())

    def fake_get_terminal_size(fd=None):
        if fd == 42:
            return os.terminal_size((133, 44))
        raise OSError('not a tty')

    monkeypatch.setattr(pytui.os, 'get_terminal_size', fake_get_terminal_size)
    assert term.get_size() == os.terminal_size((133, 44))


# ─── Deliberate degradation at the supported floor ───────────────────────

# Pane widths the shell hands the status bar on an 80 column terminal.
FLOOR_LEFT_WIDTH = 32
FLOOR_RIGHT_WIDTH = 32


def test_status_panes_match_the_floor_layout(shell, monkeypatch):
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((80, 24)))
    shell._relayout()

    assert shell._status_left.width == FLOOR_LEFT_WIDTH
    assert shell._status_right.width == FLOOR_RIGHT_WIDTH


def test_status_middle_is_screen_centered_at_the_floor(shell, monkeypatch):
    """The mode label must sit on the terminal center, not the pane center.

    Equal side panes put the middle pane's center at the screen center.
    """
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((80, 24)))
    shell._relayout()

    middle = shell._status_middle
    assert middle.x + middle.width / 2 == 40


def test_status_middle_fits_every_view_name_at_the_floor(shell, monkeypatch):
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((80, 24)))
    shell._relayout()

    assert shell._status_middle.width >= len('status:splash')


@pytest.fixture
def status_bar():
    return StatusBar(animator=Animator(), style=DEFAULT_PALETTE)


def _plain(win):
    return Text(win.buffer[0]).strip_ansi()


def test_status_bar_at_80_columns_keeps_the_radio_minimum(
        shell, monkeypatch):
    """`:channel 10` must leave cf, bw and gain visible at the floor."""
    monkeypatch.setattr(
        pytui.Terminal, 'get_size',
        lambda self: os.terminal_size((80, 24)))
    shell.daemon.apply('freq', str(CHANNEL_TO_FREQ[10] * 1_000_000))
    shell.daemon.apply('bw', '20000000')
    shell.daemon.apply('gain', 'fast')
    shell._relayout()
    shell._update_status_bar()

    out = _plain(shell._status_right)
    assert '2.457 GHz (10)' in out
    assert '20 MHz' in out
    assert 'fast' in out


def test_left_pane_shows_daemon_and_tx_glyphs_at_floor(status_bar):
    """The left pane is ambient glyphs only — no connection text."""
    win = StyledWindow(0, 0, FLOOR_LEFT_WIDTH, 1)
    status_bar._update_left(win, False, True, {})

    out = _plain(win)
    assert DEFAULT_PALETTE['glyph_daemon'] in out
    assert DEFAULT_PALETTE['glyph_tx'] in out
    assert 'SHESHA' not in out
    assert len(out) == FLOOR_LEFT_WIDTH


def test_right_pane_keeps_frequency_at_floor(status_bar):
    """Frequency, bandwidth and gain are the minimum radio state."""
    win = StyledWindow(0, 0, FLOOR_RIGHT_WIDTH, 1)
    state = {'freq': '5180000000', 'bw': '20000000',
             'gain': '50', 'agc_mode': 'manual'}
    status_bar._update_right(win, state)

    out = _plain(win)
    assert '5.18 GHz' in out
    assert '20 MHz' in out
    assert '50dB' in out
    assert len(out) == FLOOR_RIGHT_WIDTH


def test_right_pane_drops_channel_before_bandwidth(status_bar):
    """Channel is optional; bandwidth is part of the minimum."""
    win = StyledWindow(0, 0, 30, 1)
    state = {'freq': '2457000000', 'bw': '20000000',
             'gain': 'fast', 'agc_mode': 'fast'}
    status_bar._update_right(win, state)

    out = _plain(win)
    assert '2.457 GHz' in out
    assert '(10)' not in out
    assert '20 MHz' in out
    assert 'fast' in out


def test_right_pane_keeps_channel_when_it_fits_at_floor(status_bar):
    win = StyledWindow(0, 0, FLOOR_RIGHT_WIDTH, 1)
    state = {'freq': '2457000000', 'bw': '20000000',
             'gain': 'fast', 'agc_mode': 'fast'}
    status_bar._update_right(win, state)

    out = _plain(win)
    assert '2.457 GHz (10)' in out
    assert '20 MHz' in out
    assert 'fast' in out
    assert not re.search(r'\d\d:\d\d', out)


def test_right_pane_drops_clock_before_radio_state(status_bar):
    """A 90 column terminal loses the clock but keeps bandwidth."""
    win = StyledWindow(0, 0, 31, 1)
    state = {'freq': '5180000000', 'bw': '20000000',
             'gain': '50', 'agc_mode': 'manual'}
    status_bar._update_right(win, state)

    out = _plain(win)
    assert '5.18 GHz' in out
    assert '20 MHz' in out
    assert '50dB' in out
    # No clock, and no separator left dangling where it used to be.
    assert not re.search(r'\d\d:\d\d', out)
    assert out.rstrip().endswith('50dB')


def test_right_pane_keeps_everything_when_it_fits(status_bar):
    win = StyledWindow(0, 0, 42, 1)
    state = {'freq': '5180000000', 'bw': '20000000',
             'gain': '50', 'agc_mode': 'manual'}
    status_bar._update_right(win, state)

    out = _plain(win)
    assert '5.18 GHz' in out
    assert '20 MHz' in out
    assert '50dB' in out
    assert re.search(r'\d\d:\d\d', out)
