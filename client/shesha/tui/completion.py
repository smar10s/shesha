"""Filesystem path completion and expansion for the TUI command line.

Handles directory drilling (no trailing space on dirs), quoting for
paths with spaces, and prefix matching with OS path semantics.  Also
expands a single path argument into concrete files at execution time:
directories list their immediate files, shell wildcards glob, and a
plain path passes through if it exists.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Callable


def _tilde_root(partial: str) -> tuple[str, str] | None:
    """Split a leading ``~/`` or ``~user/`` into typed and expanded forms.

    Returns None when there is no slash yet or the home cannot be resolved.
    """
    if not partial.startswith('~'):
        return None
    head, sep, _rest = partial.partition(os.sep)
    if not sep:
        return None
    expanded = os.path.expanduser(head)
    if expanded == head:
        return None
    return head, expanded


def complete_path(partial: str) -> list[str]:
    """Return filesystem paths matching a partial path prefix.

    If partial is empty, lists the current directory.  Directories get a
    trailing separator so tabbing drills into them without extra typing.
    A leading tilde is resolved for listing but kept in the candidates,
    so they still prefix-match what the user typed.
    """
    if partial == '~':
        return ['~/'] if os.path.isdir(os.path.expanduser('~')) else []
    tilde = _tilde_root(partial)
    if tilde:
        typed_root, fs_root = tilde
        listing = fs_root + partial[len(typed_root):]
    else:
        listing = os.path.expanduser(partial)
    if not listing:
        dirname = '.'
        basename = ''
    else:
        dirname = os.path.dirname(listing) or '.'
        basename = os.path.basename(listing)
    try:
        entries = os.listdir(dirname)
    except OSError:
        return []
    entries.sort(key=lambda e: (e.startswith('.'), e))
    matches = []
    for entry in entries:
        if entry.startswith(basename):
            full = os.path.join(dirname, entry)
            if os.path.isdir(full):
                full += os.sep
            if tilde:
                full = typed_root + full[len(fs_root):]
            matches.append(full)
    return matches


def is_directory_completion(path: str) -> bool:
    """True if this completion is a directory (ends with separator)."""
    return path.endswith(os.sep)


def has_glob(pattern: str) -> bool:
    """True if *pattern* contains shell wildcard metacharacters."""
    return any(ch in pattern for ch in '*?[')


def expand_paths(pattern: str) -> list[str]:
    """Expand one path argument to concrete files on disk.

    - an existing directory (``foo`` or ``foo/``) yields its immediate,
      non-hidden files, sorted;
    - a wildcard pattern (``foo/*.mp3``) yields its glob matches that are
      files, sorted;
    - a plain path yields itself if it exists.

    Directories are never returned: callers queue or filter files.  The
    expansion is deliberately unfiltered — the caller decides which of
    the returned files are acceptable.
    """
    pattern = os.path.expanduser(pattern)
    if not pattern:
        return []

    if os.path.isdir(pattern):
        try:
            entries = os.listdir(pattern)
        except OSError:
            return []
        return [os.path.join(pattern, name)
                for name in sorted(entries)
                if not name.startswith('.')
                and os.path.isfile(os.path.join(pattern, name))]

    return sorted({p for p in glob.glob(pattern) if os.path.isfile(p)})


@dataclass(frozen=True)
class Completion:
    """A completion span: replace buffer[start:end] with one candidate."""

    start: int
    end: int
    candidates: list[str]


def common_prefix(candidates: list[str]) -> str:
    if not candidates:
        return ''
    prefix = candidates[0]
    for candidate in candidates[1:]:
        while not candidate.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def tokenize(buffer: str) -> list[tuple[str, int, int]]:
    """Return (token, start, end) for each whitespace-delimited run."""
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r'\S+', buffer)]


def token_at(buffer: str, cursor: int) -> tuple[int, int]:
    """Return the span of the token under *cursor*.

    A cursor sitting after whitespace starts a new, empty token.  A cursor
    at the end of a token still belongs to that token.
    """
    if cursor <= 0:
        return cursor, cursor
    if (buffer[cursor - 1].isspace()
            and (cursor == len(buffer) or buffer[cursor].isspace())):
        return cursor, cursor
    for _token, start, end in tokenize(buffer):
        if start <= cursor <= end:
            return start, end
    return cursor, cursor


class CompletionEngine:
    """Menu-complete state machine over a raw command buffer.

    Command-agnostic: it never splits on whitespace. Cycling is keyed on
    the whole buffer string, so candidates containing spaces work.
    """

    def __init__(self,
                 is_drillable: Callable[[str], bool] | None = None) -> None:
        self._is_drillable = is_drillable or (lambda s: False)
        self._candidates: list[str] = []
        self._start = 0
        self._end = 0
        self._idx = 0
        self._last_result: tuple[str, int] | None = None

    def reset(self) -> None:
        self._candidates = []
        self._start = 0
        self._end = 0
        self._idx = 0
        self._last_result = None

    def on_tab(self, buffer: str, cursor: int,
               complete_fn: Callable[[str, int], Completion],
               reverse: bool = False) -> tuple[str, int]:
        """Complete or cycle. Returns the updated buffer and cursor."""
        if self._last_result == (buffer, cursor) and len(self._candidates) > 1:
            step = -1 if reverse else 1
            self._idx = (self._idx + step) % len(self._candidates)
        else:
            completion = complete_fn(buffer, cursor)
            end = max(completion.end, cursor)
            partial = buffer[completion.start:cursor]
            matches = [c for c in completion.candidates if c.startswith(partial)]
            if not matches:
                return buffer, cursor

            prefix = common_prefix(matches)
            if len(matches) > 1 and len(prefix) > len(partial):
                result = (buffer[:completion.start] + prefix
                          + buffer[end:])
                new_cursor = completion.start + len(prefix)
                self._candidates = []
                self._last_result = (result, new_cursor)
                return result, new_cursor

            self._candidates = matches
            self._start = completion.start
            self._end = end
            self._idx = len(matches) - 1 if reverse else 0

        candidate = self._candidates[self._idx]
        result = buffer[:self._start] + candidate + buffer[self._end:]
        new_cursor = self._start + len(candidate)
        if (len(self._candidates) == 1 and self._end == len(buffer)
                and not self._is_drillable(candidate)):
            # A space is only offered when the next argument slot has
            # candidates.  A leaf command such as :stop_tx does not.
            if complete_fn(result + ' ', new_cursor + 1).candidates:
                result += ' '
                new_cursor += 1
        self._end = self._start + len(candidate)
        self._last_result = (result, new_cursor)
        return result, new_cursor
