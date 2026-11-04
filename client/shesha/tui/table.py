"""A scrolling table widget built on pytui's Window.

Divides a container into columns, supports a header row, row striping,
a highlighted row, per-column and per-cell styling, and scrolling over
large datasets.

Styling is composed once at render time from plain cell values, so no
ANSI nesting is involved.  The composition order is base row style, then
column style, then per-cell style.  The highlighted row replaces all
three, which keeps a selection from being quietly overridden by muted
columns or cell accents.
"""
from __future__ import annotations
from .pytui import (
    Window, Text, display_width, sanitize_text, truncate_display,
)

# A cell is either plain text or (text, style override).
Cell = str | tuple[str, dict]


class Table:
    DEFAULT_FORMAT = {
        'justify': 'left',
        'justify_character': ' '
    }

    def __init__(
        self,
        container: Window,
        widths,
        *,
        header: list[str] | None = None,
        header_style: dict | None = None,
        header_format: dict | None = None,
        row_style: dict | None = None,
        alt_row_style: dict | None = None,
        highlight_style: dict | None = None,
    ) -> None:
        self.striped = True
        self.row_style = row_style or {'bg': 0x000000}
        self.alt_row_style = alt_row_style or {'bg': 0x2c2c2c}
        self.highlight_style = highlight_style or {'bg': 0x808080}
        self.draw_header = False
        self._header_columns: list[Window] = []
        self.column_styles: list[dict] = [{} for _ in widths]
        self._highlight: int | None = None

        header_style = header_style or {'bold': True}
        header_format = header_format or {'justify': 'center'}
        self._header_style = header_style
        self._header_format = header_format

        if header:
            row, container = container.hsplit(1)
            self.draw_header = True
            self._header_columns = row.vsplit(*widths)[:len(widths)]
            for value, column in zip(header, self._header_columns):
                column.append_line(
                    Text(self.format_cell(value, header_format, column)).style(
                        header_style))

        self.columns = container.vsplit(*widths)[:len(widths)]
        self._visible_rows = container.height
        self.column_formats = [self.DEFAULT_FORMAT.copy() for _x in widths]
        self._rows: list[list[Cell]] = []
        self._scroll_offset = 0

    # ─── Properties ──────────────────────────────────────────────────────

    @property
    def offset(self) -> int:
        return self._scroll_offset

    @property
    def visible_rows(self) -> int:
        return self._visible_rows

    @property
    def max_scroll(self) -> int:
        return max(0, len(self._rows) - self._visible_rows)

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def highlight(self) -> int | None:
        return self._highlight

    # ─── Scroll ───────────────────────────────────────────────────────────

    def scroll_to(self, idx: int) -> None:
        self._scroll_offset = max(0, min(idx, self.max_scroll))
        self._render()

    def scroll_up(self, n: int = 1) -> None:
        self.scroll_to(self._scroll_offset - n)

    def scroll_down(self, n: int = 1) -> None:
        self.scroll_to(self._scroll_offset + n)

    def scroll_page_up(self) -> None:
        self.scroll_to(self._scroll_offset - self._visible_rows)

    def scroll_page_down(self) -> None:
        self.scroll_to(self._scroll_offset + self._visible_rows)

    # ─── Styling ──────────────────────────────────────────────────────────

    def set_column_style(self, column: int, style: dict) -> None:
        """Merge *style* into the per-column overrides."""
        self.column_styles[column].update(style)

    def set_highlight(self, index: int | None) -> None:
        """Mark *index* as the selected row; its style outranks overrides."""
        self._highlight = index

    @staticmethod
    def split_cell(cell: Cell) -> tuple[str, dict]:
        """Return a cell's plain text and its per-cell style override."""
        if isinstance(cell, tuple):
            value, style = cell
            return str(value), style or {}
        return str(cell), {}

    def _base_style(self, row_index: int) -> dict:
        if row_index == self._highlight:
            return self.highlight_style
        if self.striped and row_index % 2:
            return self.alt_row_style
        return self.row_style

    def style_row(self, row: list[Cell], style: dict | None = None,
                  overrides: bool = True) -> list[str]:
        """Render every cell of *row* to a single styled string.

        *style* is the base; column styles merge over it and per-cell
        styles over those.  With *overrides* false only the base applies,
        which lets a selected row's highlight win outright.
        """
        styled = []
        for ci, (column, fmt, cell) in enumerate(
                zip(self.columns, self.column_formats, row)):
            value, cell_style = self.split_cell(cell)
            merged = dict(style or {})
            if overrides:
                merged.update(self.column_styles[ci])
                merged.update(cell_style)
            styled.append(
                Text(self.format_cell(value, fmt, column)).style(merged))
        return styled

    # ─── Formatting ───────────────────────────────────────────────────────

    def set_column_format(self, column: int, fmt: dict) -> None:
        self.column_formats[column].update(fmt)

    def format_cell(self, value: str, fmt: dict, column: Window) -> str:
        char = fmt.get('justify_character', ' ')
        raw = sanitize_text(str(value))
        width = display_width(raw)
        if width > column.width:
            raw = truncate_display(raw, column.width)
            width = display_width(raw)
        pad = (column.width - width) * char
        if 'left' == fmt['justify']:
            return raw + pad
        elif 'right' == fmt['justify']:
            return pad + raw
        else:
            return pad[:len(pad) // 2] + raw + pad[len(pad) // 2:]

    # ─── Data / Render ────────────────────────────────────────────────────

    def update(self, rows: list[list[Cell]]) -> None:
        self._rows = rows
        self._scroll_offset = max(0, min(self._scroll_offset, self.max_scroll))
        self._render()

    def _render(self) -> None:
        self.clear()
        visible = self._rows[self._scroll_offset:
                             self._scroll_offset + self._visible_rows]
        for vi, row in enumerate(visible):
            ai = self._scroll_offset + vi
            selected = ai == self._highlight
            styled = self.style_row(row, self._base_style(ai),
                                    overrides=not selected)
            for column, value in zip(self.columns, styled):
                column.append_line(value)

    def clear(self) -> None:
        for column in self.columns:
            column.clear()

    def draw(self) -> None:
        if self.draw_header:
            for column in self._header_columns:
                column.draw()
            self.draw_header = False
        for column in self.columns:
            column.draw()
