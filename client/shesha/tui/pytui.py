"""pytui — terminal UI primitives for shesha."""
from __future__ import annotations
from typing import Callable, Self
import sys
import math
import re
import os
import select
import signal
import threading
import tty
import termios
import tempfile
import traceback
import unicodedata


# capture state at import time so multiple instances can reset to the same
try:
    termattrs = termios.tcgetattr(sys.stdin)
except (termios.error, ValueError):
    termattrs = None  # no TTY (e.g. piped, testing)


# Compiled once because the render loop measures and clips every line.
ANSI_SEQUENCE = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]')
ANSI_RESET = '\x1b[0m'

# Every escape this module does not emit itself.  OSC matters most: it can
# retitle the window or write the clipboard.  CSI can move the cursor or
# erase the screen.  Match the whole sequence, payload included.
CONTROL_SEQUENCE = re.compile(
    r'(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]'          # CSI
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?'       # OSC
    r'|\x1b[PX^_][^\x1b]*(?:\x1b\\)?'            # DCS/SOS/PM/APC
    r'|\x1b[@-Z\\-_]'                            # other two-byte escape
    r'|\x9d[^\x07\x9c\x1b]*(?:\x07|\x9c)?'       # C1 OSC
)
SGR_SEQUENCE = re.compile(r'(?:\x1b\[|\x9b)[0-9;:]*m')


def sanitize_text(text: str) -> str:
    """Return *text* with terminal commands and non-printable controls gone.

    Use on anything that came from outside the UI: SSIDs, ID3 tags, daemon
    error strings.  Only printable characters survive, plus the zero-width
    joiner that builds emoji sequences.
    """
    text = CONTROL_SEQUENCE.sub('', text)
    return ''.join(ch for ch in text if ch.isprintable() or ch == _ZWJ)


def sanitize_styled(text: str) -> str:
    """Strip every escape except the SGR styling this module emits.

    Render-boundary guard for strings that already carry UI styling.  SGR
    only changes colours; it cannot move the cursor, retitle the window, or
    write the clipboard.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        sgr = SGR_SEQUENCE.match(text, i)
        if sgr:
            out.append(sgr.group())
            i = sgr.end()
            continue
        control = CONTROL_SEQUENCE.match(text, i)
        if control:
            i = control.end()
            continue
        ch = text[i]
        if ch.isprintable() or ch == _ZWJ:
            out.append(ch)
        i += 1
    return ''.join(out)


def char_width(ch: str) -> int:
    """Terminal columns occupied by a single character.

    Wide characters (CJK, most emoji) take two columns and combining
    marks take none.  East-asian ambiguous characters are counted as one,
    matching the common terminal default.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


_ZWJ = '\u200d'
_VS16 = '\ufe0f'


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_extend(ch: str) -> bool:
    """Return True when *ch* attaches to the preceding character."""
    code = ord(ch)
    return (
        unicodedata.combining(ch) != 0
        or unicodedata.category(ch) in ('Mn', 'Mc', 'Me')
        or ch == _ZWJ
        or 0xFE00 <= code <= 0xFE0F          # variation selectors
        or 0xE0100 <= code <= 0xE01EF        # variation selectors supplement
        or 0x1F3FB <= code <= 0x1F3FF        # emoji skin tones
        or 0xE0020 <= code <= 0xE007F        # emoji tag characters
    )


def _continues_cluster(previous: str, ch: str) -> bool:
    if not previous:
        return False
    if _is_extend(ch) or previous[-1] == _ZWJ:
        return True
    return (len(previous) == 1 and _is_regional_indicator(ch)
            and _is_regional_indicator(previous))


def graphemes(text: str):
    """Yield user-perceived characters (grapheme clusters), approximated."""
    cluster: list[str] = []
    for ch in text:
        if cluster and _continues_cluster(''.join(cluster), ch):
            cluster.append(ch)
            continue
        if cluster:
            yield ''.join(cluster)
        cluster = [ch]
    if cluster:
        yield ''.join(cluster)


def grapheme_width(cluster: str) -> int:
    """Terminal columns occupied by one grapheme cluster."""
    if not cluster:
        return 0
    if _VS16 in cluster:
        return 2
    if _ZWJ in cluster:
        base = [char_width(ch) for ch in cluster
                if ch != _ZWJ and unicodedata.category(ch) not in
                ('Mn', 'Mc', 'Me')]
        return max(base, default=0)
    if any(0x1F3FB <= ord(ch) <= 0x1F3FF for ch in cluster):
        return max(2, char_width(cluster[0]))
    return sum(char_width(ch) for ch in cluster)


def display_width(text: str) -> int:
    """Terminal columns occupied by *text*, ignoring ANSI styling."""
    return sum(grapheme_width(cluster)
               for cluster in graphemes(Text(text).strip_ansi()))


def truncate_display(text: str, width: int) -> str:
    """Clip plain text to *width* columns, never splitting a grapheme."""
    out = []
    col = 0
    for cluster in graphemes(text):
        columns = grapheme_width(cluster)
        if col + columns > width:
            break
        out.append(cluster)
        col += columns
    return ''.join(out)


class Canvas:
    """A canvas where each "pixel" is a braille code.

    Computer graphics style origin at top left.

    Shamelessy based on https://github.com/asciimoo/drawille but faster.
    """

    BRAILLE_CODES = (
        (0x01, 0x08),
        (0x02, 0x10),
        (0x04, 0x20),
        (0x40, 0x80)
    )

    def __init__(self, cols: int, rows: int) -> None:
        """Creates new canvas.

        Args:
            cols: Canvas width in characters (2x as many points).
            rows: Canvas height in characters (4x as many points).
        """
        self.cols = cols        # size in characters
        self.rows = rows
        self.width = cols * 2   # size in braille points
        self.height = rows * 4
        self.codes = [0]*(cols*rows)

    def set(self, x: int, y: int) -> None:
        """Sets a braille point.

        Args:
            x: X coordinate.
            y: Y coordinate.
        """
        (col, row) = (x//2, y//4)
        if row >= 0 and col >= 0 and row < self.rows and col < self.cols:
            self.codes[row*self.cols + col] |= self.BRAILLE_CODES[y % 4][x % 2]

    def line(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """Draws a line between two braille points.

        Args:
            x1: Start X coordinate.
            y1: Start Y coordinate.
            x2: End X coordinate.
            y2: End Y coordinate.
        """
        xd = x2 - x1
        yd = y2 - y1
        steps = int(max(abs(xd), abs(yd)))
        for i in range(0, steps):
            x = round(x1 + (xd / steps * i))
            y = round(y1 + (yd / steps * i))
            self.set(x, y)

    def draw_row(self, row: int) -> str:
        i = row * self.cols
        chars = [chr(0x2800 + self.codes[i + x]) for x in range(self.cols)]
        return ''.join(chars)

    def draw(self) -> str:
        """Returns the canvas as text."""
        return "\n".join([self.draw_row(row) for row in range(self.rows)])


class Plot:
    """A cartesian plane with origin at bottom left.

    Coordinate range is independent from display dimensions.
    """

    def __init__(
        self,
        cols: int,
        rows: int,
        minx: float,
        miny: float,
        maxx: float,
        maxy: float
    ) -> None:
        """Creates a new plot for a given display size and coordinate range.

        Args:
            cols: Plot width in characters.
            rows: Plot heigh in characters.
            minx: Starting X value.
            miny: Starting Y value.
            maxx: Ending X value.
            maxy: Ending Y value.
        """
        self.canvas = Canvas(cols, rows)
        self.minx = minx
        self.miny = miny
        self.maxx = maxx
        self.maxy = maxy

    def translate(self, v: float, a: float, b: float, s: float) -> int:
        return round((v - a) / (b - a) * s)

    def translatexy(self, x: float, y: float) -> tuple[int, int]:
        # flip y to match common plots
        cx = self.translate(x, self.minx, self.maxx, self.canvas.width - 1)
        cy = self.translate(y, self.maxy, self.miny, self.canvas.height - 1)
        return (cx, cy)

    def point(self, x: float, y: float) -> None:
        """Draws a point.

        Args:
            x: X coordinate.
            y: Y coordinate.
        """
        self.canvas.set(*self.translatexy(x, y))

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Draws a line between two coordinate points.

        Args:
            x1: Start X coordinate.
            y1: Start Y coordinate.
            x2: End X coordinate.
            y2: End Y coordinate.
        """
        self.canvas.line(*self.translatexy(x1, y1), *self.translatexy(x2, y2))

    def draw(self) -> str:
        """Returns plot as text."""
        return self.canvas.draw()


class PixelCanvas:
    """A canvas where each "pixel" is a unicode half block element.

    Computer graphics style origin at top left.
    """

    def __init__(
        self,
        width: int,
        height: int,
        background: tuple[int, int, int] = (0, 0, 0)
    ) -> None:
        """Creates new canvas.

        Args:
            width: Canvas width in "pixels" (characters).
            height: Canvas height in "pixels" (characters*2).
            background: The default pixel color as (R,G,B) tuple.
        """
        self.width = width
        self.height = height
        self.data = [background]*(width*height)

    def set_data(self, data: list[tuple[int, int, int]]) -> None:
        """Sets the entire canvas from a width*height wide list."""
        self.data = data

    def set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        """Sets a pixel.

        Args:
            x: X coordinate.
            y: Y coordinate.
            color: Pixel color as (R,G,B) tuple.
        """
        self.data[(y * self.width) + x] = color

    def draw(self) -> str:
        """Returns the canvas as text."""
        lines = []
        for y in range(self.height):
            if 0 == y % 2:
                line = ''
                for x in range(self.width):
                    top = self.data[(y * self.width) + x]
                    bottom = self.data[((y+1) * self.width) + x]
                    line += Text('▀').style({'fg': top, 'bg': bottom})
                lines.append(line)
        return "\n".join(lines)


# While Screen owns fds 1/2, the renderer must keep writing to the real tty
# even though sys.stdout now points at the log.  Screen sets this to the tty
# handle; every Terminal (there is one per Window) renders through it.
# None means "resolve sys.stdout at write time", which keeps
# contextlib.redirect_stdout working for snapshot/table rendering.
_render_stream = None


class Terminal:
    """Utility methods for terminal interaction."""

    def _stream(self):
        return _render_stream if _render_stream is not None else sys.stdout

    def flush(self) -> None:
        """Flushes output."""
        self._stream().flush()

    def write(self, string: str) -> None:
        """Writes string to output buffer."""
        self._stream().write(string)

    def clear(self) -> None:
        """Clears the terminal."""
        self.write("\x1b[2J")

    def set_cursor(self, x: int, y: int) -> None:
        """Sets the cursor position for next write.

        Args:
            x: Cursor column.
            y: Cursor row.
        """
        self.write("\x1b[%d;%dH" % (y + 1, x + 1))

    def hide_cursor(self) -> None:
        """Hides cursor."""
        self.write("\x1b[?25l")

    def show_cursor(self) -> None:
        """Shows cursor."""
        self.write("\x1b[?25h")

    def reset_colors(self) -> None:
        """Resets background and foreground colors."""
        self.write("\x1b[0m")

    def fullscreen(self) -> None:
        """Switches to alt screen, clears, hides cursor, moves to top left."""
        self.write("\x1b[?1049h")  # alt screen
        self.clear()
        self.hide_cursor()
        self.set_cursor(0, 0)

    def restore(self) -> None:
        """Restores from alt screen."""
        self.write("\x1b[?1049l")
        self.flush()

    def reset(self) -> None:
        """Resets colors and shows the cursor."""
        self.reset_colors()
        self.show_cursor()

    def get_size(self) -> os.terminal_size:
        """Return the terminal size, querying a real tty fd.

        Not the default fd (1): while the TUI owns fds 1/2 they point at the
        log, so ``os.get_terminal_size()`` fails and the render silently pins
        to the 80x24 fallback.  Query the render stream's tty, then stdin.
        """
        fds = []
        try:
            fds.append(self._stream().fileno())
        except (AttributeError, OSError, ValueError):
            pass
        try:
            fds.append(sys.stdin.fileno())
        except (OSError, ValueError):
            pass
        for fd in fds:
            try:
                return os.get_terminal_size(fd)
            except OSError:
                continue
        return os.terminal_size((80, 24))  # fallback for non-TTY

    def get_columns(self) -> int:
        """Returns current terminal width."""
        return self.get_size().columns

    def get_lines(self) -> int:
        """Returns current terminal height."""
        return self.get_size().lines


class Window:
    """A rectangle of text positioned somewhere on the screen.

    Content can be set directly or appended or prepended, scrolling up or
    down respectively.

    Windows can also be vertically or horizontally partitioned into new ones
    for more complex layouts.
    """

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        """Creates a new window.

        Args:
            x: Starting column.
            y: Starting row.
            w: Width in characters.
            h: Height in rows.
        """
        self.terminal = Terminal()
        self.x = x
        self.y = y
        self.width = w
        self.height = h
        self.buffer: list[str] = []
        self._dirty = True

    def create(self, x, y, w, h) -> Self:
        return type(self)(x, y, w, h)

    def clear(self) -> None:
        """Clears the content of this window."""
        self.buffer = []
        self._dirty = True

    def truncate_line(self, line: str, width: int) -> str:
        """Clip a line to a column count, keeping its styling intact.

        Escape sequences cost no columns, so styled text clips at the same
        place plain text would. Wide characters cost two columns and are
        never split: one that does not fit is dropped along with the rest
        of the line. A clipped line always closes its styling.
        """
        out: list[str] = []
        col = 0
        i = 0
        styled = False
        clipped = False
        while i < len(line):
            escape = ANSI_SEQUENCE.match(line, i)
            if escape:
                out.append(escape.group())
                i = escape.end()
                styled = True
                continue
            end = i + 1
            while end < len(line) and not ANSI_SEQUENCE.match(line, end):
                if not _continues_cluster(line[i:end], line[end]):
                    break
                end += 1
            cluster = line[i:end]
            columns = grapheme_width(cluster)
            if col + columns > width:
                clipped = True
                break
            out.append(cluster)
            col += columns
            i = end
        if clipped and styled:
            out.append(ANSI_RESET)
        return ''.join(out)

    def justify_line(self, line: str, justify: str = 'left') -> str:
        length = display_width(line)
        if length > self.width:
            line = self.truncate_line(line, self.width)

        width = self.width + (len(line) - length)  # make up for ansi codes
        if 'left' == justify:
            return line.ljust(width)
        elif 'right' == justify:
            return line.rjust(width)
        else:
            return line.center(width)

    def append_line(self, line: str, justify: str = 'left') -> None:
        """Appends a line, scrolling if necessary.

        Args:
            line: Line to append. Clipped if wider than the window.
            justify: Justification, either left, right or center. Default left.
        """
        if len(self.buffer) >= self.height:     # scroll up
            self.buffer = self.buffer[1:]
        self.buffer.append(self.justify_line(sanitize_styled(line), justify))
        self._dirty = True

    def prepend_line(self, line: str, justify: str = 'left') -> None:
        """Prepends a line, scrolling if necessary.

        Args:
            line: Line to prepend. Clipped if wider than the window.
            justify: Justification, either left, right or center. Default left.
        """
        if len(self.buffer) >= self.height:     # scroll down
            self.buffer = self.buffer[:-1]
        self.buffer.insert(0, self.justify_line(sanitize_styled(line), justify))
        self._dirty = True

    def draw_line(self, line: str) -> None:
        self.terminal.write(line)

    def draw(self) -> None:
        """Writes current buffer to terminal (but does not flush.)
        Skips if content hasn't changed since last draw.
        """
        if not self._dirty:
            return
        for i in range(self.height):
            self.terminal.set_cursor(self.x, self.y + i)
            have = i < len(self.buffer)
            self.draw_line(self.buffer[i] if have else ''.ljust(self.width))
        self._dirty = False

    def update_buffer(self, buffer: list[str]) -> None:
        """Updates current buffer.

        Buffer must be of the correct dimensions.  Terminal commands are
        stripped at this boundary; only SGR styling survives.
        """
        self.buffer = [sanitize_styled(line) for line in buffer]
        self._dirty = True

    def update_content(self, content: str) -> None:
        """Updates buffer from multiline string."""
        lines = [sanitize_styled(line) for line in content.splitlines()]
        self.update_buffer(list(map(self.justify_line, lines)))

    def splitrange(self, total: int, *args) -> list[int]:
        available = total
        values = []

        for i in args:
            if isinstance(i, float):
                i = int(total * i)

            if isinstance(i, int):
                if available - i < 0:
                    raise Exception('not enough room to split')
                values.append(i)
                available -= i
            elif i is None:
                values.append(-1)
            else:
                raise Exception(f'invalid split point type {type(i)}')

        if available == 0 and values.count(-1) == 0:
            return values

        if -1 not in values:
            values.append(-1)

        blanks = values.count(-1)

        if available < blanks:
            raise Exception('not enough room to split')

        # overflow into the last available spot
        last_blank = max(i for i, w in enumerate(values) if w == -1)
        values[last_blank] = math.ceil(available/blanks)

        # fill the remaining
        return [available//blanks if x == -1 else x for x in values]

    def hsplit(self, *args) -> list[Self]:
        """Horizontally splits this window into two or more new windows.

        Args:
            *args: One or more ints, floats or None. Ints are interpreted as
                number of characters, floats as percentage, None as the
                equally distributed remainder, if any. None is implied as the
                last argument if not explicitly included otherwise (i.e. any
                unaccounted for remainder is returned with a last window.)
                Examples:
                (0.2)        Returns windows sized (20%, 80%).
                (0.2, None)  Equivalent to above.
                (0.2, 0.8)   Equivalent to above.
                (10)         Returns windows sized (10, any remainder)
                (1, None, 1) Returns windows sized (1, any remainder, 1)
                (1, None, None, 1) Like above, but with remainder distributed
                    between two windows: (1, remainder, remainder, 1)
                (1, 0.2)     Returns windows sized (1, 20% of original, any
                    remainder)
        """
        windows = []
        y = self.y
        for height in self.splitrange(self.height, *args):
            windows.append(self.create(self.x, y, self.width, height))
            y += height
        return windows

    def vsplit(self, *args) -> list[Self]:
        """Vertically splits this window into two or more new windows.

        Args:
            *args: One or more ints, floats or None. Ints are interpreted as
                number of characters, floats as percentage, None as the
                equally distributed remainder, if any. None is implied as the
                last argument if not explicitly included otherwise (i.e. any
                unaccounted for remainder is returned with a last window.)
                Examples:
                (0.2)        Returns windows sized (20%, 80%).
                (0.2, None)  Equivalent to above.
                (0.2, 0.8)   Equivalent to above.
                (10)         Returns windows sized (10, any remainder)
                (1, None, 1) Returns windows sized (1, any remainder, 1)
                (1, None, None, 1) Like above, but with remainder distributed
                    between two windows: (1, remainder, remainder, 1)
                (1, 0.2)     Returns windows sized (1, 20% of original, any
                    remainder)
        """
        windows = []
        x = self.x
        for width in self.splitrange(self.width, *args):
            windows.append(self.create(x, self.y, width, self.height))
            x += width
        return windows


class StyledWindow(Window):
    """A window with ANSI style applied to content."""

    def __init__(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        style: dict | None = None
    ) -> None:
        """Creates a new styled window.

        Args:
            x:      Starting row.
            y:      Starting line.
            w:      Width.
            h:      Height.
            style:  A dictionary of style options. See Text.style for details.
        """
        super().__init__(x, y, w, h)
        self.style = style if style is not None else {}

    def create(self, x, y, w, h) -> StyledWindow:
        return type(self)(x, y, w, h, self.style)

    def update_style(self, style: dict) -> None:
        self.style = style

    def draw_line(self, line: str) -> None:
        ansi_style = Text('').style(self.style, '')
        line = ansi_style + line.replace("\x1b[0m", "\x1b[0m" + ansi_style)
        line = self.justify_line(line)
        self.terminal.write(line)
        self.terminal.reset_colors()


class Text:
    """Utility class for dealing with ANSI text."""

    def __init__(self, string: str) -> None:
        """Creates a new text instance.

        Args:
            string: A string.
        """
        self.string = string

    def strip_ansi(self) -> str:
        """Strips terminal commands and non-printable controls."""
        return sanitize_text(self.string)

    def split_rgb(self, c: int) -> tuple[int, int, int]:
        return ((c >> 16) & 0x0000ff, (c >> 8) & 0x0000ff, c & 0x0000ff)

    def rgb(self, c) -> tuple[int, int, int]:
        if type(c) is int:
            return self.split_rgb(c)
        return c

    def style(self, options: dict, terminator: str = "\x1b[0m") -> str:
        """Applies an ANSI style to the string.

        Args:
            options:    A dictionary of style options. Available:
                fg and bg
                    foreground and background color, as RGB int (0x102030)
                    or separate values in a tuple ((0x10, 0x20, 0x30))
                bold
                faint
                italic
                underline
                blink
                negative
                crossed
            terminator: A style terminator. Defaults to ANSI color reset.

        Returns:
            String with ANSI codes.
        """
        string = self.string
        codes = []
        if 'fg' in options:
            codes.append("\x1b[38;2;%d;%d;%dm" % self.rgb(options['fg']))
        if 'bg' in options:
            codes.append("\x1b[48;2;%d;%d;%dm" % self.rgb(options['bg']))
        if 'bold' in options and options['bold']:
            codes.append("\033[1m")
        if 'faint' in options and options['faint']:
            codes.append("\033[2m")
        if 'italic' in options and options['italic']:
            codes.append("\033[3m")
        if 'underline' in options and options['underline']:
            codes.append("\033[4m")
        if 'blink' in options and options['blink']:
            codes.append("\033[5m")
        if 'negative' in options and options['negative']:
            codes.append("\033[7m")
        if 'crossed' in options and options['crossed']:
            codes.append("\033[9m")
        return ''.join(codes) + string + terminator if codes else string


class Keyboard:
    """A non-blocking key dispatcher.

    Uses Python threading and POSIX termios to read individual keypresses.
    Dispatches to registered listeners on each keypress.

    Arrow keys, Page Up/Down, Home/End are returned as named constants.
    Escape is distinguished from ANSI sequences via a short read timeout.
    Shift is readable as uppercase. Ctrl+key comes through as chr(1-26).
    """
    UP = 'up'
    DOWN = 'down'
    RIGHT = 'right'
    LEFT = 'left'
    PAGE_UP = 'page_up'
    PAGE_DOWN = 'page_down'
    HOME = 'home'
    END = 'end'
    ESCAPE = 'escape'
    UNKNOWN = 'unknown'
    BACKSPACE = '\x7f'
    DELETE = 'delete'
    ENTER = '\x0a'
    TAB = '\x09'
    SHIFT_TAB = 'shift_tab'

    def __init__(self) -> None:
        self.thread = None
        self.listeners: list[Callable[[str], None]] = []
        # Called with any exception a listener raises.  Without it a
        # listener bug would print a traceback over the alt screen and
        # stop input; see Shell._listener_error.
        self.on_error: Callable[[BaseException], None] | None = None

    def disable_line_buffering(self) -> None:
        if sys.stdin.isatty():
            tty.setcbreak(sys.stdin)

    def reset(self) -> None:
        """Resets stdin to the state captured at module load time."""
        if termattrs is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, termattrs)

    def _read_with_timeout(self, timeout: float = 0.05) -> str | None:
        """Read one char from stdin with timeout. Returns None on timeout."""
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            return os.read(sys.stdin.fileno(), 1).decode(errors='replace')
        return None

    def read_ansi(self) -> str:
        """Parse an ANSI escape sequence after ESC has been read.

        A CSI sequence is ``ESC [ params intermediates final``.  Modified
        keys carry parameters (``ESC[3;2~`` is Shift+Delete), so consume
        the whole sequence before decoding.  Reading a fixed number of
        bytes leaves the parameter text to be typed as ordinary input.
        """
        c = self._read_with_timeout(0.02)
        if c is None:
            return self.ESCAPE  # bare Escape key
        if c == 'O':
            # SS3 sequence (ESC O A and friends), used by some terminals.
            c = self._read_with_timeout(0.02)
            return {
                'A': self.UP,
                'B': self.DOWN,
                'C': self.RIGHT,
                'D': self.LEFT,
                'H': self.HOME,
                'F': self.END,
            }.get(c, self.UNKNOWN)
        if c != '[':
            return self.UNKNOWN

        params = ''
        while True:
            c = self._read_with_timeout(0.02)
            if not c:
                return self.UNKNOWN
            if '\x40' <= c <= '\x7e':
                return self._csi_key(params, c)
            if not '\x20' <= c <= '\x3f':
                return self.UNKNOWN
            params += c

    @classmethod
    def _csi_key(cls, params: str, final: str) -> str:
        """Map a complete CSI sequence to a key constant."""
        if final == 'Z':
            return cls.SHIFT_TAB
        if final == 'A':
            return cls.UP
        if final == 'B':
            return cls.DOWN
        if final == 'C':
            return cls.RIGHT
        if final == 'D':
            return cls.LEFT
        if final == 'H':
            return cls.HOME
        if final == 'F':
            return cls.END
        if final == '~':
            first = params.split(';', 1)[0]
            return {
                '1': cls.HOME,
                '3': cls.DELETE,
                '4': cls.END,
                '5': cls.PAGE_UP,
                '6': cls.PAGE_DOWN,
            }.get(first, cls.UNKNOWN)
        return cls.UNKNOWN

    def read(self) -> None:
        fd = sys.stdin.fileno()
        while True:
            try:
                data = os.read(fd, 1)
            except OSError:
                return
            if not data:
                return  # EOF: stop rather than spin on an empty key
            c = data.decode(errors='replace')
            if '\x1b' == c:
                c = self.read_ansi()
            elif c == '\r':
                c = '\n'
            elif c == '\x08':
                c = self.BACKSPACE  # Ctrl-H
            self._dispatch(c)

    def _dispatch(self, c: str) -> None:
        """Deliver one keypress to every listener, containing any failure.

        A listener bug must not kill the reader thread or spew a traceback
        over the alt screen.  The full traceback goes to fds 1/2 (a log file
        while the TUI runs; see Screen), and the handler gets the exception.
        """
        for fn in self.listeners:
            try:
                fn(c)
            except Exception as exc:  # noqa: BLE001 - keep input alive
                traceback.print_exc()
                if self.on_error is not None:
                    self.on_error(exc)

    def add_listener(self, fn: Callable[[str], None]) -> None:
        """Adds a keyboard listener invoked on each keypress.

        Args:
            fn: Callback receiving a character or named key constant.
        """
        self.listeners.append(fn)

    def listen(self, fn: Callable[[str], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None) -> None:
        """Starts listening for keyboard input in a background thread.

        Args:
            fn: Optional callback to register before starting.
            on_error: Optional sink for exceptions raised by listeners.
        """
        if fn:
            self.add_listener(fn)
        if on_error is not None:
            self.on_error = on_error
        if not self.thread:
            self.disable_line_buffering()
            self.thread = threading.Thread(target=self.read, daemon=True)
            self.thread.start()


class Screen:
    """Root window manager. Owns the terminal lifecycle and layout tree.

    Handles alt-screen, SIGWINCH, and provides a build/draw cycle.
    Typical usage:

        screen = Screen()
        screen.start()
        # build layout from screen.root (a StyledWindow)
        header, body = screen.root.hsplit(1, None)
        ...
        screen.draw()  # draws all dirty windows
        screen.stop()
    """

    def __init__(self, log_path: str | None = None) -> None:
        self.terminal = Terminal()
        self.keyboard = Keyboard()
        self._windows: list[Window] = []
        self._layout_fn: Callable[[StyledWindow], list[Window]] | None = None
        self._started = False
        self._resize_pending = False
        # Stray output (children, logging, thread tracebacks) is collected
        # here while the TUI owns fds 1/2.
        self._log_path = log_path or os.path.join(
            tempfile.gettempdir(), 'shesha-tui.log')
        self._saved_out: int | None = None
        self._saved_err: int | None = None
        self._log = None
        self._tty_out = None
        cols, lines = self.terminal.get_columns(), self.terminal.get_lines()
        self.root = StyledWindow(0, 0, cols, lines)

    def start(self) -> None:
        """Enter fullscreen mode, take over fds 1/2, install handlers."""
        self.terminal.fullscreen()
        self.terminal.flush()
        self._take_over_output()
        self._started = True
        signal.signal(signal.SIGWINCH, self._on_resize)

    def stop(self) -> None:
        """Restore terminal state and the process's fds."""
        if self._started:
            self.terminal.reset()
            self.terminal.restore()
            self.keyboard.reset()
            self._started = False
        self._restore_output()

    # ─── Output ownership ────────────────────────────────────────────────
    #
    # While the TUI runs, fds 1/2 belong to it.  A child process, logging's
    # last-resort handler, or a thread traceback would otherwise flash a
    # line over the alt screen.  Point both at a log file and give the
    # renderer a private handle on the real tty.  See docs/tui.md.

    def _take_over_output(self) -> None:
        global _render_stream
        try:
            log = open(self._log_path, 'a', encoding='utf-8')
        except OSError:
            log = open(os.devnull, 'w', encoding='utf-8')
        self._log = log
        sys.stdout.flush()
        sys.stderr.flush()
        self._saved_out = os.dup(1)
        self._saved_err = os.dup(2)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        self._tty_out = os.fdopen(os.dup(self._saved_out), 'w',
                                  encoding='utf-8', errors='replace',
                                  buffering=1)
        _render_stream = self._tty_out

    def _restore_output(self) -> None:
        global _render_stream
        if self._saved_out is None:
            return
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._saved_out, 1)
        os.dup2(self._saved_err, 2)
        _render_stream = None
        for fd in (self._saved_out, self._saved_err):
            try:
                os.close(fd)
            except OSError:
                pass
        self._saved_out = self._saved_err = None
        if self._tty_out is not None:
            try:
                self._tty_out.close()
            except OSError:
                pass
            self._tty_out = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def layout(self, fn: Callable[[StyledWindow], list[Window]]) -> None:
        """Register a layout function and build the initial layout.

        The function receives the root StyledWindow and should return
        a flat list of leaf windows to be drawn.
        """
        self._layout_fn = fn
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild layout from current terminal size."""
        cols, lines = self.terminal.get_columns(), self.terminal.get_lines()
        self.root = StyledWindow(0, 0, cols, lines)
        if self._layout_fn:
            self._windows = self._layout_fn(self.root)
            # Mark all dirty after rebuild
            for w in self._windows:
                w._dirty = True

    def _on_resize(self, signum, frame) -> None:
        """SIGWINCH handler: set flag for main loop to handle.

        Signal handlers must not call IO (stdout.write/flush) because
        the signal can arrive mid-write, causing reentrant BufferedWriter.
        """
        self._resize_pending = True

    def check_resize(self) -> None:
        """Check and handle pending resize. Call from main loop."""
        if self._resize_pending:
            self._resize_pending = False
            self._rebuild()
            if self._started:
                self.terminal.clear()
                self.draw()

    def draw(self) -> None:
        """Draw all dirty windows, then flush."""
        for w in self._windows:
            w.draw()
        self.terminal.flush()

    def force_redraw(self) -> None:
        """Mark all windows dirty and redraw."""
        for w in self._windows:
            w._dirty = True
        self.draw()

    @property
    def width(self) -> int:
        return self.root.width

    @property
    def height(self) -> int:
        return self.root.height


def shutdown():
    """A shutdown function that restores terminal state and exits."""
    Terminal().reset()
    Terminal().restore()
    Keyboard().reset()
    sys.exit(0)
