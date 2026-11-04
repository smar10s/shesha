"""Modal input state machine.

InputHandler manages the vim normal-mode semantics (digit accumulation,
escape, mode switching) and command-mode editing (backspace, enter, escape).

Returns InputResult describing what happened — does NOT execute actions.
This keeps it testable without needing a full Shell + Daemon + Screen.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InputResult:
    """Structured result of key handling."""
    kind: str
    # kind values:
    #   'enter_command_mode' — user typed ':'
    #   'exit_command_mode'  — user pressed Escape in command mode
    #   'execute'            — user pressed Enter in command mode
    #   'normal_action'      — a normal mode action key (with count)
    #   'clear'              — Escape in normal mode (clears count)
    #   'tab'                — Tab in command mode
    #   'history_back'       — Up arrow in command mode
    #   'history_forward'    — Down arrow in command mode
    #   'typing'             — printable char in command mode (buffer updated)
    #   'backspace'          — backspace in command mode (buffer updated)
    #   'delete'             — delete key in command mode (buffer updated)
    #   'cursor'             — cursor moved in command mode
    #   'exit_on_empty_bs'   — backspace on empty buffer exits command mode
    #   'noop'               — no action needed
    key: str = ''
    count: int = 1
    had_digits: bool = False
    command: str = ''
    reverse: bool = False


# Key constants matching pytui.Keyboard (passed in at construction time
# so InputHandler doesn't import pytui directly).
_DEFAULT_KEYS = {
    'escape': 'escape',
    'enter': '\x0a',
    'tab': '\x09',
    'shift_tab': 'shift_tab',
    'backspace': '\x7f',
    'delete': 'delete',
    'home': 'home',
    'end': 'end',
    'up': 'up',
    'down': 'down',
    'left': 'left',
    'right': 'right',
}


class InputHandler:
    """Modal input state machine.

    Handles the vim normal-mode semantics (digit accumulation, escape,
    mode switching) and command-mode editing (backspace, enter, escape,
    tab, arrows, printable characters).
    """

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        self.command_mode = False
        self.command_buf = ''
        self.cursor = 0
        self.count_buf = ''
        k = keys or _DEFAULT_KEYS
        self._ESCAPE = k['escape']
        self._ENTER = k['enter']
        self._TAB = k['tab']
        self._SHIFT_TAB = k['shift_tab']
        self._BACKSPACE = k['backspace']
        self._DELETE = k['delete']
        self._HOME = k['home']
        self._END = k['end']
        self._UP = k['up']
        self._DOWN = k['down']
        self._LEFT = k['left']
        self._RIGHT = k['right']

    def handle_key(self, key: str) -> InputResult:
        """Process a key press and return what happened."""
        if self.command_mode:
            return self._handle_command_key(key)
        return self._handle_normal_key(key)

    def _handle_normal_key(self, key: str) -> InputResult:
        if key == ':':
            self.command_mode = True
            self.command_buf = ''
            return InputResult(kind='enter_command_mode')

        if key == self._ESCAPE:
            self.count_buf = ''
            return InputResult(kind='clear')

        # Digit accumulation
        if key.isdigit():
            self.count_buf += key
            return InputResult(kind='noop')

        # Consume count
        count = int(self.count_buf) if self.count_buf else 1
        had_digits = bool(self.count_buf)
        self.count_buf = ''

        return InputResult(kind='normal_action', key=key, count=count,
                           had_digits=had_digits)

    def _handle_command_key(self, key: str) -> InputResult:
        if key == self._ENTER:
            cmd = self.command_buf.strip()
            self.command_mode = False
            self.command_buf = ''
            self.cursor = 0
            if cmd:
                return InputResult(kind='execute', command=cmd)
            return InputResult(kind='exit_command_mode')

        if key == self._ESCAPE:
            self.command_mode = False
            self.command_buf = ''
            self.cursor = 0
            return InputResult(kind='exit_command_mode')

        if key == self._BACKSPACE:
            if self.cursor > 0:
                self.command_buf = (self.command_buf[:self.cursor - 1]
                                    + self.command_buf[self.cursor:])
                self.cursor -= 1
                return InputResult(kind='backspace')
            if not self.command_buf:
                self.command_mode = False
                return InputResult(kind='exit_on_empty_bs')
            return InputResult(kind='noop')

        if key == self._DELETE:
            if self.cursor < len(self.command_buf):
                self.command_buf = (self.command_buf[:self.cursor]
                                    + self.command_buf[self.cursor + 1:])
                return InputResult(kind='delete')
            return InputResult(kind='noop')

        if key == self._LEFT:
            self.cursor = max(0, self.cursor - 1)
            return InputResult(kind='cursor')

        if key == self._RIGHT:
            self.cursor = min(len(self.command_buf), self.cursor + 1)
            return InputResult(kind='cursor')

        if key in (self._HOME, '\x01'):
            self.cursor = 0
            return InputResult(kind='cursor')

        if key in (self._END, '\x05'):
            self.cursor = len(self.command_buf)
            return InputResult(kind='cursor')

        if key == self._TAB:
            return InputResult(kind='tab', reverse=False)

        if key == self._SHIFT_TAB:
            return InputResult(kind='tab', reverse=True)

        if key == self._UP:
            return InputResult(kind='history_back')

        if key == self._DOWN:
            return InputResult(kind='history_forward')

        # Printable characters
        if len(key) == 1 and key.isprintable():
            self.command_buf = (self.command_buf[:self.cursor]
                                + key
                                + self.command_buf[self.cursor:])
            self.cursor += 1
            return InputResult(kind='typing', key=key)

        return InputResult(kind='noop')
