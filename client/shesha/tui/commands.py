"""Command dispatch, tab completion, and history.

The dispatcher owns the completion engine and history.  Resolution and
candidate selection live in command_model: one tree drives both.
"""
from __future__ import annotations

from collections import deque
from typing import Callable

from .command_model import Command, Resolution, complete_buffer, resolve
from .completion import Completion, CompletionEngine, is_directory_completion


class CommandDispatcher:
    def __init__(self, root: Command,
                 get_active_command: Callable[[], Command | None]) -> None:
        self._root = root
        self._get_active = get_active_command
        self._command_history: deque[str] = deque(maxlen=100)
        self._history_index = -1
        self._saved_input = ''
        self._engine = CompletionEngine(is_drillable=is_directory_completion)

    @property
    def root(self) -> Command:
        return self._root

    def resolve(self, cmd: str) -> Resolution | None:
        return resolve(self._root, self._get_active(), cmd)

    def reset_tab_state(self) -> None:
        self._engine.reset()

    def complete(self, buffer: str, cursor: int) -> Completion:
        return complete_buffer(self._root, self._get_active(), buffer, cursor)

    def on_tab(self, buffer: str, cursor: int,
               reverse: bool = False) -> tuple[str, int]:
        return self._engine.on_tab(buffer, cursor, self.complete,
                                   reverse=reverse)

    def push_history(self, cmd: str,
                     persist: Callable[[list[str]], None] | None = None,
                     max_len: int = 100) -> None:
        """Add command to history (dedup consecutive)."""
        if self._command_history and self._command_history[-1] == cmd:
            return
        self._command_history.append(cmd)
        self._history_index = -1
        if persist and max_len > 0:
            persist(list(self._command_history)[-max_len:])

    def history_back(self, command_buf: str) -> str:
        """Navigate history backwards. Returns new command buffer."""
        if not self._command_history:
            return command_buf
        if self._history_index == -1:
            self._saved_input = command_buf
            self._history_index = len(self._command_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return command_buf
        self.reset_tab_state()
        return self._command_history[self._history_index]

    def history_forward(self, command_buf: str) -> str:
        """Navigate history forwards. Returns new command buffer."""
        if self._history_index == -1:
            return command_buf
        if self._history_index < len(self._command_history) - 1:
            self._history_index += 1
            self.reset_tab_state()
            return self._command_history[self._history_index]
        self._history_index = -1
        self.reset_tab_state()
        return self._saved_input
