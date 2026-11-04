"""Tests for the headless view snapshot tool.

The tool is the verification loop for styling work, so it has to be
trustworthy itself.  These tests pin three properties: the plain grid
faithfully reflects what would appear on screen, the per-cell colours
distinguish correct from incorrect styling, and repeated renders are
byte-identical.
"""
import contextlib
import io
import re

import pytest

from shesha.tui.pytui import StyledWindow, Text, display_width
from shesha.tui.snapshot import (
    VIEWS, render_view, Snapshot, CellStyle, _feed, _parse_grid, _parse_sgr,
)
from shesha.tui.theme import STYLES, DEFAULT_PALETTE

# The supported terminal floor, a comfortable size, and a large one.
SIZES = ((80, 24), (120, 30), (200, 50))

# ── basic rendering ──────────────────────────────────────────────────


def test_render_fm_rx_returns_snapshot():
    snap = render_view('fm:rx', width=120, height=30)
    assert isinstance(snap, Snapshot)
    assert snap.width == 120
    assert snap.height == 30


def test_plain_grid_has_expected_dimensions():
    snap = render_view('fm:rx', width=120, height=30)
    lines = snap.plain.split('\n')
    assert len(lines) == 30
    assert all(len(line) == 120 for line in lines)


def test_dial_shows_tuned_frequency():
    """The tuned frequency must be legible in the plain text."""
    snap = render_view('fm:rx', width=120, height=30)
    assert '100.1 MHz' in snap.plain
    assert '^' in snap.plain


def test_ansi_output_is_truecolor():
    snap = render_view('fm:rx', width=120, height=30)
    assert '\x1b[38;2;' in snap.ansi


# ── per-cell colour tracking (C1) ───────────────────────────────────


def test_foreground_counts_reflect_cells_not_escapes():
    """Cell counts must distinguish correct from inverted hierarchy."""
    snap = render_view('fm:rx', width=120, height=30)
    total = sum(snap.foregrounds.values())
    assert total == 120 * 30, 'every cell must have a counted foreground'


def test_cell_style_returns_styling_at_position():
    """Individual cell queries must report the active colours."""
    snap = render_view('fm:rx', width=120, height=30)
    style = snap.cell_style(0, 0)
    assert isinstance(style, CellStyle)
    assert style.fg is not None, 'top-left cell should have a foreground'


def test_oscilloscope_renders_braille():
    """Audio feed must produce braille glyphs in the waveform pane."""
    snap = render_view('fm:rx', width=120, height=30)
    braille = [ch for ch in snap.plain if '\u2800' <= ch <= '\u28ff']
    assert len(braille) > 0, 'no braille glyphs found — oscilloscope missing'


def test_background_counts_are_populated():
    snap = render_view('fm:rx', width=120, height=30)
    total = sum(snap.backgrounds.values())
    assert total > 0


def test_cell_counts_cover_every_position():
    """Foreground and background totals must equal the cell count."""
    snap = render_view('fm:rx', width=120, height=30)
    total_fg = sum(snap.foregrounds.values())
    total_bg = sum(snap.backgrounds.values())
    assert total_fg == 120 * 30
    assert total_bg == 120 * 30


def test_colors_reports_distinct_foregrounds():
    snap = render_view('fm:rx', width=120, height=30)
    assert len(snap.foregrounds) >= 1


# ── combined SGR parsing (C2) ───────────────────────────────────────


def test_parse_sgr_combined_fg_bold_bg():
    """A single SGR with reset, fg, bold, and bg must decode all fields."""
    fg, bg, attrs = _parse_sgr(
        '0;38;2;8;255;255;1;40', None, None, frozenset())
    assert fg == (8, 255, 255)
    assert attrs == frozenset({'bold'})
    # code 40 is not a truecolour bg — it's a legacy 8-colour code.
    # Our parser only tracks 48;2;r;g;b, so bg stays at whatever reset set.
    assert bg is None


def test_parse_sgr_multiple_attrs():
    """Bold + underline in one sequence."""
    fg, bg, attrs = _parse_sgr('1;4', None, None, frozenset())
    assert 'bold' in attrs
    assert 'underline' in attrs


def test_parse_sgr_reset_clears_everything():
    fg, bg, attrs = _parse_sgr(
        '0', (1, 2, 3), (4, 5, 6), frozenset({'bold'}))
    assert fg is None
    assert bg is None
    assert attrs == frozenset()


def test_parse_sgr_truecolour_bg():
    fg, bg, attrs = _parse_sgr(
        '48;2;16;20;36', None, None, frozenset())
    assert bg == (16, 20, 36)


# ── determinism ──────────────────────────────────────────────────────


def test_render_is_deterministic():
    """Determinism is verified per view — the harness does not guarantee it."""
    a = render_view('fm:rx', width=120, height=30)
    b = render_view('fm:rx', width=120, height=30)
    assert a.ansi == b.ansi


# ── size matrix ──────────────────────────────────────────────────────


@pytest.mark.parametrize('width,height', SIZES)
def test_renders_at_every_supported_size(width, height):
    """80x24 is the floor; content wider than the pane must clip, not raise."""
    snap = render_view('fm:rx', width=width, height=height)
    lines = snap.plain.split('\n')
    assert len(lines) == height
    assert all(len(line) == width for line in lines)


@pytest.mark.parametrize('theme', sorted(STYLES))
def test_every_theme_renders_its_own_background(theme):
    snap = render_view('fm:rx', width=120, height=30, theme=theme)
    assert STYLES[theme]['bg'] in snap.backgrounds, snap.summary()


# ── fm:tx ────────────────────────────────────────────────────────────


def test_fm_tx_renders():
    snap = render_view('fm:tx', width=120, height=30)
    assert 'now playing' not in snap.plain
    assert '\u2500 playing ' in snap.plain
    assert 'playlist' in snap.plain
    assert len(snap.foregrounds) >= 1


def test_fm_tx_render_is_deterministic():
    """The transmit view reads the clock; guard against it leaking in."""
    a = render_view('fm:tx', width=120, height=30)
    b = render_view('fm:tx', width=120, height=30)
    assert a.ansi == b.ansi


# ── every registered view ────────────────────────────────────────────


@pytest.mark.parametrize('view', VIEWS)
def test_every_view_renders(view):
    """A view in the registry must produce a full truecolor grid."""
    snap = render_view(view, width=120, height=30)
    lines = snap.plain.split('\n')
    assert len(lines) == 30
    assert all(display_width(line) == 120 for line in lines)
    assert '\x1b[38;2;' in snap.ansi
    assert snap.foregrounds, snap.summary()


@pytest.mark.parametrize('view', VIEWS)
def test_every_view_is_deterministic(view):
    """Synthetic feeds must not leak the wall clock into the render."""
    a = render_view(view, width=120, height=30)
    b = render_view(view, width=120, height=30)
    assert a.ansi == b.ansi


@pytest.mark.parametrize('view', VIEWS)
def test_every_view_survives_the_floor_size(view):
    """Content wider than an 80x24 pane must clip, not raise."""
    snap = render_view(view, width=80, height=24)
    lines = snap.plain.split('\n')
    assert len(lines) == 24
    assert all(display_width(line) == 80 for line in lines)


# ── visibility guard ─────────────────────────────────────────────────


def _is_pixel_glyph(ch: str) -> bool:
    """Block elements are pixels; fg == bg there means a solid colour."""
    return '\u2580' <= ch <= '\u259f'


@pytest.mark.parametrize('theme', sorted(STYLES))
@pytest.mark.parametrize('view', VIEWS)
def test_no_text_is_drawn_in_its_own_background(view, theme):
    """Invisible text was a real regression; guard every rendered view."""
    snap = render_view(view, width=120, height=30, theme=theme)
    for r in range(snap.height):
        for c in range(snap.width):
            ch, style = snap.cells[r][c]
            if not ch.strip() or _is_pixel_glyph(ch):
                continue
            assert style.fg != style.bg, (
                f'{view} ({theme}): {ch!r} at ({r},{c}) drawn in its own '
                f'background')


def test_fm_rx_quiet_audio_trace_is_accent(monkeypatch):
    """Quiet audio is silence, not an absent station: trace stays accent."""
    monkeypatch.setattr('shesha.tui.snapshot._synthetic_audio',
                        lambda: [0.01] * 2000)
    snap = render_view('fm:rx', width=120, height=30)
    accent = DEFAULT_PALETTE['accent']
    trace_cells = [
        (r, c)
        for r in range(4, 25)
        for c in range(snap.width)
        if '\u2800' <= snap.cells[r][c][0] <= '\u28ff'
    ]
    assert trace_cells, snap.plain
    assert all(snap.cell_style(r, c).fg == accent for r, c in trace_cells), (
        snap.summary())


# ── synthetic feeds ──────────────────────────────────────────────────


def test_status_splash_shows_signal_health():
    snap = render_view('status:splash', width=120, height=30)
    assert 'SNR' in snap.plain
    assert 'floor/peak' in snap.plain


def test_status_splash_floor_and_peak_share_one_row():
    snap = render_view('status:splash', width=120, height=30)
    rows = [line for line in snap.plain.splitlines()
            if 'floor' in line or 'peak' in line]
    assert len(rows) == 1, rows
    assert re.search(r'floor/peak\s+-?\d+\.\d+/-?\d+\.\d+ dB', rows[0])


def test_status_splash_omits_stream_health():
    """Stream health lives on the waterfall's bottom bar now."""
    snap = render_view('status:splash', width=120, height=30)
    assert 'coverage' not in snap.plain
    assert not re.search(r'\bstream\b', snap.plain)  # 'bitstream' still shown


def test_status_splash_shows_connected_endpoint():
    snap = render_view('status:splash', width=120, height=30)
    assert re.search(r'connected\s+192\.168\.2\.1:5555', snap.plain)


def test_status_splash_shows_offline_endpoint():
    snap = render_view('status:splash', width=120, height=30, state='offline')
    assert re.search(r'offline\s+192\.168\.2\.1:5555', snap.plain)


def test_status_splash_reports_deimos_with_wifi():
    snap = render_view('status:splash', width=120, height=30)
    assert 'bitstream' in snap.plain
    assert 'deimos (wifi)' in snap.plain


def test_status_splash_reports_styx_with_none():
    snap = render_view('status:splash', width=120, height=30, state='styx')
    assert 'styx (none)' in snap.plain


def test_status_splash_omits_removed_device_rows():
    snap = render_view('status:splash', width=120, height=30)
    assert 'firmware' not in snap.plain
    assert 'accel' not in snap.plain
    assert '3fa9c1d2' not in snap.plain.lower()


def test_status_splash_shows_device_and_apps():
    snap = render_view('status:splash', width=120, height=30)
    assert 'bitstream' in snap.plain
    assert 'deimos' in snap.plain
    assert ':status' in snap.plain


def test_status_splash_shows_every_registered_app():
    from shesha.tui.registry import APP_CLASSES

    snap = render_view('status:splash', width=120, height=30)
    for cls in APP_CLASSES:
        assert cls.name in snap.plain
        assert cls.description in snap.plain


def test_waterfall_shows_spectrum_and_frequency_axis():
    snap = render_view('viz:waterfall', width=120, height=30)
    assert '5.18 GHz' in snap.plain
    braille = [ch for ch in snap.plain if '\u2800' <= ch <= '\u28ff']
    assert braille, 'PSD pane should contain braille'


def test_waterfall_bottom_bar_shows_coverage_and_fps():
    """The waterfall owns stream health: coverage and delivery rate."""
    snap = render_view('viz:waterfall', width=120, height=30)
    assert 'coverage' in snap.plain
    assert '99.7%' in snap.plain
    assert 'fps' in snap.plain
    assert '50/50 Hz' in snap.plain
    assert 'workers 2' in snap.plain


def test_waterfall_clock_is_injectable():
    """Row pacing comes from the injected clock, not the wall clock."""
    from shesha.tui.viz import WaterfallView

    frames = 12 * 2 + 4
    ticks = iter([float(1000 + i) for i in range(frames + 2)])
    view = WaterfallView(clock=lambda: next(ticks))
    view.theme_changed(DEFAULT_PALETTE)
    view.layout(StyledWindow(0, 0, 60, 12, {}))
    _feed(view, 'viz:waterfall', 12)
    assert view._last_row_time == 1000.0 + frames - 1


def test_viz_const_renders_density_heatmap():
    snap = render_view('viz:const', width=80, height=24)
    assert '\u2580' in snap.plain          # half-block plot glyph
    assert snap.backgrounds                  # heatmap paints colour


def test_viz_const_axis_uses_the_plot_foreground():
    """The centre axes must use the surface text tone, not the muted tone."""
    snap = render_view('viz:const', width=80, height=24)
    plot_fg = DEFAULT_PALETTE['plot_fg']
    assert plot_fg in snap.foregrounds or plot_fg in snap.backgrounds


def test_wifi_scan_shows_aps_clients_and_detail():
    snap = render_view('wifi:scan', width=120, height=30)
    assert 'Home' in snap.plain
    assert 'Unassociated' in snap.plain
    assert 'wpa2-psk' in snap.plain


def test_wifi_scan_stations_show_receive_channel():
    snap = render_view('wifi:scan', width=120, height=30)
    sta_rows = [line for line in snap.plain.split('\n') if 'STA' in line]
    assert sta_rows
    assert all('36' in row for row in sta_rows), sta_rows


def test_wifi_scan_handles_wide_ssids():
    """Wide SSIDs clip on a cell boundary instead of shifting the panes."""
    for width in (80, 120):
        snap = render_view('wifi:scan', width=width, height=24)
        lines = snap.plain.split('\n')
        assert all(display_width(line) == width for line in lines), snap.plain
    snap = render_view('wifi:scan', width=120, height=30)
    assert '东京都无线局域网' in snap.plain


def test_wifi_traffic_shows_breakdown_table():
    snap = render_view('wifi:traffic', width=120, height=30)
    assert 'Type' in snap.plain
    assert 'ALL' in snap.plain
    assert 'FCS OK%' in snap.plain


@pytest.mark.parametrize('name', ['wifi:scan', 'wifi:traffic'])
def test_wifi_views_keep_an_idle_tx_bar(name):
    snap = render_view(name, width=80, height=24)
    bar = snap.plain.split('\n')[-1]
    assert 'tx' in bar
    assert '--' in bar


@pytest.mark.parametrize('name', ['wifi:scan', 'wifi:traffic'])
@pytest.mark.parametrize('width', [100, 80, 60])
def test_wifi_tx_bar_scales_to_width(name, width):
    snap = render_view(name, width=width, height=24, state='tx')
    lines = snap.plain.split('\n')
    assert all(display_width(line) == width for line in lines)
    bar = lines[-1]
    assert 'tx' in bar
    assert 'deauth' in bar
    if width == 100:
        assert 'aa:bb:cc:dd:ee:ff' in bar
        assert '11:22:33:44:55:66' in bar
    elif width == 60:
        assert '…' in bar


@pytest.mark.parametrize('name', ['wifi:scan', 'wifi:traffic'])
def test_wifi_tx_bar_colours_the_tx_word(name):
    palette = DEFAULT_PALETTE
    idle = render_view(name, width=80, height=24)
    bar = idle.plain.split('\n')[-1]
    assert idle.cell_style(idle.height - 1, bar.find('tx')).fg == palette['dim']

    active = render_view(name, width=80, height=24, state='tx')
    bar = active.plain.split('\n')[-1]
    assert active.cell_style(active.height - 1, bar.find('tx')).fg == palette['warn']


# ── styling hierarchy ────────────────────────────────────────────────


def _find_text(snap: Snapshot, needle: str) -> tuple[int, int]:
    """Return (row, col) of the first occurrence of *needle*."""
    for row, line in enumerate(snap.plain.split('\n')):
        col = line.find(needle)
        if col >= 0:
            return row, col
    raise AssertionError(f'{needle!r} not found in snapshot')


def test_wifi_scan_secondary_columns_recede():
    """Ch, Type and Last mute to plot_fg; SSID and BSSID stay bright."""
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:scan', width=120, height=30)
    row, col = _find_text(snap, '10:20:30:40:50:60')
    for i in range(len('10:20:30:40:50:60')):
        assert snap.cell_style(row, col + i).fg == palette['fg']
    fgs = {snap.cell_style(row, c).fg for c in range(snap.width)}
    assert palette['plot_fg'] in fgs


def test_wifi_scan_hidden_ssid_is_muted():
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:scan', width=120, height=30)
    row, col = _find_text(snap, '<hidden>')
    assert snap.cell_style(row, col).fg == palette['plot_fg']


def test_wifi_scan_detail_header_and_fields_are_styled():
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:scan', width=120, height=30)
    row, col = _find_text(snap, '\u2500 AP Detail \u2500')
    style = snap.cell_style(row, col)
    assert style.fg == palette['accent']
    assert 'bold' in style.attrs

    row, col = _find_text(snap, 'SSID:')
    assert snap.cell_style(row, col).fg == palette['plot_fg']
    assert snap.cell_style(row, col + 11).fg == palette['fg']


def test_wifi_scan_unknown_values_are_muted():
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:scan', width=120, height=30)
    row, col = _find_text(snap, 'Vendor:')
    assert snap.cell_style(row, col + 11).fg == palette['plot_fg']


def test_wifi_traffic_type_emphasis_and_muted_columns():
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:traffic', width=120, height=30)

    row, col = _find_text(snap, 'deauth')
    assert snap.cell_style(row, col).fg == palette['warn']
    assert 'bold' in snap.cell_style(row, col).attrs

    row, col = _find_text(snap, 'eapol')
    assert snap.cell_style(row, col).fg == palette['accent']

    row, _ = _find_text(snap, 'beacon')
    fgs = {snap.cell_style(row, c).fg for c in range(snap.width)}
    assert palette['plot_fg'] in fgs
    assert palette['fg'] in fgs


def test_wifi_traffic_selected_row_uses_highlight():
    palette = DEFAULT_PALETTE
    snap = render_view('wifi:traffic', width=120, height=30)
    row, col = _find_text(snap, 'ALL')
    style = snap.cell_style(row, col)
    assert style.bg == palette['accent']
    assert style.fg == palette['bg']


# ── error handling ───────────────────────────────────────────────────


def test_unknown_view_is_rejected():
    with pytest.raises(ValueError):
        render_view('nope:nope')


def test_unknown_theme_is_rejected():
    with pytest.raises(ValueError):
        render_view('fm:rx', theme='nope')


# ── summary ──────────────────────────────────────────────────────────


def test_summary_names_the_view_size_and_theme():
    snap = render_view('fm:rx', width=120, height=30, theme='tokyonight')
    assert 'fm:rx' in snap.summary()
    assert '120x30' in snap.summary()
    assert 'tokyonight' in snap.summary()


# ── grid parser unit tests ───────────────────────────────────────────


def test_grid_recovers_plain_text_from_clipped_styled_line():
    """Clipping a styled line must not leak escapes into the plain grid."""
    win = StyledWindow(0, 0, 5, 1, {'fg': (1, 2, 3)})
    win.update_buffer([Text('AAA').style({'fg': (4, 5, 6), 'bold': True})
                       + 'BBBBBB'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        win.draw()
    _grid, plain = _parse_grid(buf.getvalue(), 5, 1)
    assert plain == 'AAABB'


def test_grid_places_content_at_the_window_offset():
    """Windows draw at absolute positions; the parser must honour them."""
    win = StyledWindow(2, 1, 3, 1, {'fg': (1, 2, 3)})
    win.update_buffer(['XYZ'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        win.draw()
    _grid, plain = _parse_grid(buf.getvalue(), 6, 2)
    assert plain == '      \n  XYZ '


def test_grid_tracks_style_per_cell():
    """Each cell must carry the fg/bg/attrs active at write time."""
    win = StyledWindow(0, 0, 5, 1, {'fg': (1, 2, 3)})
    win.update_buffer([Text('AB').style({'fg': (10, 20, 30), 'bold': True})
                       + 'CD'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        win.draw()
    grid, _plain = _parse_grid(buf.getvalue(), 5, 1)
    # A and B were styled inline.
    assert grid[0][0][1].fg == (10, 20, 30)
    assert 'bold' in grid[0][0][1].attrs
    # C and D pick up the window base style after reset.
    assert grid[0][2][1].fg == (1, 2, 3)
    assert 'bold' not in grid[0][2][1].attrs


def test_grid_rejects_csi_shorthand():
    """\\x9b is not emitted by pytui; the parser must refuse it."""
    with pytest.raises(ValueError, match='CSI shorthand'):
        _parse_grid('\x1b[1;1H\x9b0m', 5, 1)


def test_grid_rejects_newline():
    with pytest.raises(ValueError, match='newlines'):
        _parse_grid('\x1b[1;1H\n', 5, 1)


def test_grid_rejects_carriage_return():
    with pytest.raises(ValueError, match='carriage return'):
        _parse_grid('\x1b[1;1H\r', 5, 1)


def test_grid_rejects_write_outside_bounds():
    with pytest.raises(ValueError, match='outside'):
        _parse_grid('\x1b[1;6HA', 5, 1)


def test_grid_rejects_unrecognised_escape():
    with pytest.raises(ValueError, match='unrecognised'):
        _parse_grid('\x1b[1;1H\x1bX', 5, 1)


# ── Task 4: FM RX visual hierarchy ──────────────────────────────────

def test_fm_rx_uses_more_than_two_fg_colors():
    """Hierarchy needs more than the 2 foregrounds of the unstyled baseline."""
    snap = render_view('fm:rx', width=120, height=30)
    assert len(snap.foregrounds) > 2, snap.summary()


def test_fm_rx_dims_dial_furniture():
    snap = render_view('fm:rx', width=120, height=30)
    assert snap.cells_with_fg(DEFAULT_PALETTE['dim']) > 0, snap.summary()


def test_fm_rx_accents_tuned_frequency():
    snap = render_view('fm:rx', width=120, height=30)
    assert snap.cells_with_fg(DEFAULT_PALETTE['accent']) > 0, snap.summary()


def test_fm_rx_uses_bold():
    snap = render_view('fm:rx', width=120, height=30)
    assert snap.attributes.get('bold', 0) > 0, snap.summary()


def test_fm_rx_text_still_legible():
    """Inline ANSI must not corrupt the plain content or widths."""
    snap = render_view('fm:rx', width=120, height=30)
    assert '100.1 MHz' in snap.plain
    assert 'Station:' in snap.plain


def test_fm_rx_renders_decoded_rds():
    snap = render_view('fm:rx', width=120, height=30)
    assert 'SHESHA' in snap.plain
    assert '8203' in snap.plain
    assert 'Hack The Planet' in snap.plain
    lines = snap.plain.split('\n')
    assert all(len(line) == 120 for line in lines)


def test_fm_rx_caret_is_accented():
    """The caret must be visually distinct from the tick marks."""
    snap = render_view('fm:rx', width=120, height=30)
    caret_style = None
    tick_style = None
    for r in range(snap.height):
        for c in range(snap.width):
            ch, style = snap.cells[r][c]
            if ch == '^' and caret_style is None:
                caret_style = style
            if ch == '|' and tick_style is None:
                tick_style = style
        if caret_style and tick_style:
            break
    assert caret_style is not None, 'no caret found'
    assert tick_style is not None, 'no tick found'
    assert caret_style.fg != tick_style.fg, f'caret={caret_style.fg}, tick={tick_style.fg}'


# ── Task 6: FM TX visual hierarchy ─────────────────────────────────


def test_fm_tx_uses_more_than_one_fg_color():
    """TX should have styling in the dial at minimum."""
    snap = render_view('fm:tx', width=120, height=30)
    assert len(snap.foregrounds) > 1, snap.summary()


def test_fm_tx_uses_bold():
    snap = render_view('fm:tx', width=120, height=30)
    assert snap.attributes.get('bold', 0) > 0, snap.summary()


def test_fm_tx_playing_marker_and_selection():
    """Playing is marked in accent; the selected row gets the highlight."""
    palette = DEFAULT_PALETTE
    snap = render_view('fm:tx', width=120, height=30, state='streaming')
    mark_row = None
    for r, line in enumerate(snap.plain.split('\n')):
        if line.startswith('\u2502 \u25b6'):
            mark_row = r
            break
    assert mark_row is not None, 'no playing marker in the playlist'
    assert snap.cell_style(mark_row, 2).fg == palette['accent']
    row, col = _find_text(snap, 'mixtape_side_a.mp3')
    assert snap.cell_style(row, col).bg == palette['accent']


# ── fm:tx streaming preset ───────────────────────────────────────────


def test_fm_tx_streaming_shows_now_playing_and_playlist():
    """The streaming preset renders the player and the queued tracks."""
    snap = render_view('fm:tx', width=120, height=30, state='streaming')
    assert 'pirate_radio.mp3' in snap.plain
    assert 'playlist (6)' in snap.plain
    assert '1:34 / 3:48' in snap.plain
    assert '\u25b6' in snap.plain


def test_fm_tx_streaming_is_deterministic():
    """Progress and elapsed time must not read the wall clock."""
    a = render_view('fm:tx', width=120, height=30, state='streaming')
    b = render_view('fm:tx', width=120, height=30, state='streaming')
    assert a.ansi == b.ansi


def test_fm_tx_streaming_shows_rds_fields():
    """The now-playing panel shows the fields we are broadcasting."""
    snap = render_view('fm:tx', width=120, height=30, state='streaming')
    assert 'PS: TESTFM' in snap.plain
    assert 'PTY: 10' in snap.plain


def test_fm_tx_streaming_shows_spectrum():
    """The streaming preset draws the half-block spectrum bar chart."""
    snap = render_view('fm:tx', width=120, height=30, state='streaming')
    assert '\u2580' in snap.plain


def test_fm_tx_idle_shows_rds_off():
    """With RDS off the panel says so rather than showing a stale name."""
    snap = render_view('fm:tx', width=120, height=30)
    assert 'RDS' in snap.plain
    assert 'off' in snap.plain
    assert 'PS: TESTFM' not in snap.plain


def test_state_preset_rejects_unknown_view_and_state():
    with pytest.raises(ValueError):
        render_view('fm:rx', state='streaming')
    with pytest.raises(ValueError):
        render_view('fm:tx', state='nope')


# ── full shell chrome ────────────────────────────────────────────────


def test_shell_render_includes_chrome():
    """--shell must draw the status bar, TX glyphs and input hints."""
    snap = render_view('fm:tx', width=80, height=24,
                       shell=True, state='streaming')
    assert DEFAULT_PALETTE['glyph_daemon'] in snap.plain
    assert DEFAULT_PALETTE['glyph_tx'] in snap.plain
    assert 'fm:tx' in snap.plain
    assert 'Space:pause' in snap.plain


def test_shell_render_shows_tx_telemetry():
    """The streaming view reports TX telemetry in its footer."""
    snap = render_view('fm:tx', width=80, height=24,
                       shell=True, state='streaming')
    assert ' depth ' in snap.plain
    assert 'underruns' in snap.plain


def test_shell_grid_covers_terminal():
    snap = render_view('fm:tx', width=80, height=24,
                       shell=True, state='streaming')
    lines = snap.plain.split('\n')
    assert len(lines) == 24
    assert all(display_width(line) == 80 for line in lines)


def test_shell_render_is_deterministic():
    """The status-bar clock must be injected, not read from the wall."""
    a = render_view('fm:tx', width=120, height=30,
                    shell=True, state='streaming')
    b = render_view('fm:tx', width=120, height=30,
                    shell=True, state='streaming')
    assert a.ansi == b.ansi


@pytest.mark.parametrize('view', VIEWS)
def test_every_view_renders_with_shell(view):
    """Any registered view can be reviewed with full terminal chrome."""
    snap = render_view(view, width=80, height=24, shell=True)
    lines = snap.plain.split('\n')
    assert len(lines) == 24
    assert all(display_width(line) == 80 for line in lines)
    assert DEFAULT_PALETTE['glyph_daemon'] in snap.plain
    assert '\x1b[38;2;' in snap.ansi


@pytest.mark.parametrize('view', ('viz:waterfall', 'viz:const'))
def test_shell_snapshot_fills_the_view_body(view):
    """--shell must feed the view after its windows exist.

    The viz views drop frames until layout has created their canvas, so a
    feed that runs first leaves the reviewed screenshot blank.
    """
    snap = render_view(view, width=100, height=30, shell=True)
    body = snap.plain.split('\n')[1:-1]
    assert sum(ch != ' ' for line in body for ch in line) > 100
