"""Table styling composition.

The table used to accept pre-styled rows and wrap them again at render
time, which nested ANSI and let a muted column or a per-cell style win
over the selection highlight.  Styling is now composed once, at render
time, from plain values: base row style, column style, cell style, with
the highlighted row replacing all three.  These tests pin that order and
the stripe coverage it depends on.
"""
import contextlib
import io
import re

from shesha.tui.pytui import StyledWindow, display_width
from shesha.tui.snapshot import _parse_grid
from shesha.tui.table import Table


_FG = re.compile(r'\x1b\[38;2;(\d+);(\d+);(\d+)m')
_BG = re.compile(r'\x1b\[48;2;(\d+);(\d+);(\d+)m')

ROW_FG = (200, 200, 200)
ROW_BG = (1, 1, 1)
ALT_BG = (2, 2, 2)
HIGHLIGHT_BG = (3, 3, 3)
HIGHLIGHT_FG = (0, 0, 0)


def _table(widths=(10, 10)) -> Table:
    root = StyledWindow(
        0, 0, sum(widths), 4, {'bg': (0, 0, 0), 'fg': (255, 255, 255)})
    return Table(
        root, list(widths),
        row_style={'bg': ROW_BG, 'fg': ROW_FG},
        alt_row_style={'bg': ALT_BG, 'fg': ROW_FG},
        highlight_style={'bg': HIGHLIGHT_BG, 'fg': HIGHLIGHT_FG},
    )


def _draw(table: Table, width: int = 20):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for column in table.columns:
            column.draw()
    return _parse_grid(buf.getvalue(), width, 4)[0]


def _fg(styled: str) -> tuple[int, int, int]:
    return tuple(map(int, _FG.search(styled).groups()))


def _bg(styled: str) -> tuple[int, int, int]:
    return tuple(map(int, _BG.search(styled).groups()))


def test_column_style_overrides_row_style():
    table = _table()
    table.set_column_style(1, {'fg': (9, 9, 9)})
    styled = table.style_row(['aa', 'bb'], table.row_style)
    assert _fg(styled[0]) == ROW_FG
    assert _fg(styled[1]) == (9, 9, 9)


def test_column_style_merges_by_key():
    table = _table()
    table.set_column_style(0, {'bold': True})
    styled = table.style_row(['aa', 'bb'], table.row_style)
    assert _fg(styled[0]) == ROW_FG
    assert '\x1b[1m' in styled[0]


def test_cell_style_overrides_column_style():
    table = _table()
    table.set_column_style(0, {'fg': (9, 9, 9)})
    styled = table.style_row([('aa', {'fg': (5, 5, 5)}), 'bb'])
    assert _fg(styled[0]) == (5, 5, 5)


def test_cell_plain_text_survives_styling():
    table = _table()
    styled = table.style_row([('hidden', {'fg': (5, 5, 5)}), 'bb'])
    assert styled[0].startswith('\x1b[38;2;5;5;5m')
    assert 'hidden' in styled[0]


def test_format_cell_pads_wide_text_by_display_width():
    table = _table()
    fmt = table.column_formats[0]
    column = table.columns[0]
    cases = (
        ('left', '中文' + ' ' * 6),
        ('right', ' ' * 6 + '中文'),
        ('center', ' ' * 3 + '中文' + ' ' * 3),
    )
    for justify, expected in cases:
        fmt['justify'] = justify
        cell = table.format_cell('中文', fmt, column)
        assert cell == expected
        assert display_width(cell) == column.width
    fmt['justify'] = 'left'


def test_format_cell_clips_wide_text_on_a_cell_boundary():
    table = _table()
    column = table.columns[0]
    cell = table.format_cell('中' * 6, table.column_formats[0], column)
    assert cell == '中' * 5
    assert display_width(cell) == column.width


def test_format_cell_drops_a_wide_character_that_does_not_fit():
    table = _table(widths=(3, 17))
    column = table.columns[0]
    cell = table.format_cell('中中中', table.column_formats[0], column)
    assert cell == '中 '
    assert display_width(cell) == 3


def test_highlight_replaces_column_and_cell_overrides():
    table = _table()
    table.set_column_style(0, {'fg': (9, 9, 9)})
    table.set_highlight(0)
    table.update([('aa', {'fg': (5, 5, 5)}), 'bb'])
    grid = _draw(table)
    assert grid[0][0][1].fg == HIGHLIGHT_FG
    assert grid[0][0][1].bg == HIGHLIGHT_BG
    assert grid[1][0][1].fg == (9, 9, 9)


def test_unselected_rows_still_get_column_styles():
    table = _table()
    table.set_column_style(1, {'fg': (9, 9, 9)})
    table.set_highlight(0)
    table.update([['aa', 'bb'], ['cc', 'dd']])
    grid = _draw(table)
    assert grid[1][11][1].fg == (9, 9, 9)


def test_stripe_background_covers_padding():
    """A short styled cell must not punch a hole in its row stripe."""
    table = _table()
    table.update([['aa', 'bb'], [('cc', {'fg': (5, 5, 5)}), 'dd']])
    grid = _draw(table)
    assert all(grid[1][c][1].bg == ALT_BG for c in range(20))
    assert all(grid[0][c][1].bg == ROW_BG for c in range(20))


def test_clear_highlight_restores_stripes():
    table = _table()
    rows = [['aa', 'bb'], ['cc', 'dd']]
    table.set_highlight(1)
    table.update(rows)
    table.set_highlight(None)
    table.update(rows)
    grid = _draw(table)
    assert grid[1][0][1].bg == ALT_BG
