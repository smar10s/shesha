"""Window text must survive a terminal narrower than its content.

The TUI previously raised when content outgrew its pane, which killed the
render on any terminal under about 110 columns. Wide characters (CJK,
emoji) count two columns and must never be split across the boundary.
"""
from shesha.tui.pytui import (
    StyledWindow, Text, char_width, display_width, truncate_display,
)


def test_truncate_plain_line():
    w = StyledWindow(0, 0, 10, 1)
    assert w.truncate_line('abcdefghijklmnop', 10) == 'abcdefghij'


def test_truncate_keeps_short_line_intact():
    w = StyledWindow(0, 0, 10, 1)
    assert w.truncate_line('abc', 10) == 'abc'


def test_truncate_styled_line_counts_visible_columns():
    """ANSI escapes must not consume columns or get cut in half."""
    w = StyledWindow(0, 0, 40, 1)
    line = (Text('AAA').style({'fg': (1, 2, 3)}) + 'BBB'
            + Text('CCC').style({'fg': (4, 5, 6)}))
    out = w.truncate_line(line, 5)
    assert Text(out).strip_ansi() == 'AAABB'
    assert out.endswith('\x1b[0m')


def test_truncate_styled_line_to_zero():
    w = StyledWindow(0, 0, 40, 1)
    line = Text('AAA').style({'fg': (1, 2, 3)})
    assert Text(w.truncate_line(line, 0)).strip_ansi() == ''


def test_justify_line_truncates_instead_of_raising():
    w = StyledWindow(0, 0, 10, 1)
    out = w.justify_line('this line is far too long for ten columns')
    assert len(Text(out).strip_ansi()) == 10


def test_justify_line_pads_short_line_to_width():
    w = StyledWindow(0, 0, 10, 1)
    assert len(Text(w.justify_line('abc')).strip_ansi()) == 10


def test_justify_line_preserves_styled_width():
    w = StyledWindow(0, 0, 20, 1)
    line = Text('hello').style({'fg': (8, 247, 254), 'bold': True})
    assert len(Text(w.justify_line(line)).strip_ansi()) == 20


# ── wide characters ──────────────────────────────────────────────────


def test_char_width_classifies_wide_and_combining():
    assert char_width('a') == 1
    assert char_width('中') == 2
    assert char_width('\U0001f4f6') == 2   # 📶
    assert char_width('\u0301') == 0       # combining acute


def test_display_width_ignores_styling():
    assert display_width('abc') == 3
    assert display_width('中文') == 4
    assert display_width(Text('中').style({'fg': (1, 2, 3)})) == 2


def test_truncate_display_never_splits_a_wide_character():
    assert truncate_display('中中中', 3) == '中'
    assert truncate_display('a中b', 3) == 'a中'
    assert truncate_display('abc', 10) == 'abc'


def test_truncate_line_never_splits_a_wide_character():
    w = StyledWindow(0, 0, 40, 1)
    assert w.truncate_line('中中中', 3) == '中'
    assert w.truncate_line('a中b', 3) == 'a中'
    assert w.truncate_line('中a', 2) == '中'


def test_truncate_styled_wide_line_closes_styling():
    w = StyledWindow(0, 0, 40, 1)
    out = w.truncate_line(Text('中文字').style({'fg': (1, 2, 3)}), 3)
    assert Text(out).strip_ansi() == '中'
    assert out.endswith('\x1b[0m')


def test_justify_line_pads_by_display_width():
    w = StyledWindow(0, 0, 6, 1)
    assert w.justify_line('中文') == '中文  '
    out = w.justify_line('中文字')
    assert Text(out).strip_ansi() == '中文字'
    assert display_width(out) == 6


# ── grapheme clusters ────────────────────────────────────────────────


def test_display_width_counts_a_grapheme_cluster_once():
    family = '\U0001f468\u200d\U0001f469\u200d\U0001f467'
    assert display_width(family) == 2
    assert display_width('\u263a\ufe0f') == 2       # emoji presentation
    assert display_width('\U0001f44d\U0001f3fd') == 2  # skin tone
    assert display_width('e\u0301') == 1            # combining accent


def test_truncate_display_never_splits_a_grapheme_cluster():
    family = '\U0001f468\u200d\U0001f469\u200d\U0001f467'
    assert truncate_display(family + 'x', 2) == family
    assert truncate_display(family + 'x', 3) == family + 'x'


def test_truncate_line_never_splits_a_grapheme_cluster():
    family = '\U0001f468\u200d\U0001f469\u200d\U0001f467'
    w = StyledWindow(0, 0, 40, 1)
    assert w.truncate_line(family + 'x', 2) == family
    assert w.truncate_line(family + 'x', 3) == family + 'x'
