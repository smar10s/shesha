"""Keyboard dispatch must contain listener failures.

A key handler raising used to print a traceback and kill the reader thread,
so all further input died with it.
"""
import pytest

from shesha.tui.pytui import Keyboard


@pytest.mark.parametrize('sequence, key', [
    (['[', '3', '~'], Keyboard.DELETE),
    (['[', '3', ';', '2', '~'], Keyboard.DELETE),
    (['[', '3', ';', '5', '~'], Keyboard.DELETE),
    (['[', '5', '~'], Keyboard.PAGE_UP),
    (['[', '5', ';', '2', '~'], Keyboard.PAGE_UP),
    (['[', '6', ';', '2', '~'], Keyboard.PAGE_DOWN),
    (['[', '4', '~'], Keyboard.END),
    (['[', '1', '~'], Keyboard.HOME),
    (['[', '1', ';', '5', 'H'], Keyboard.HOME),
    (['[', 'A'], Keyboard.UP),
    (['[', 'Z'], Keyboard.SHIFT_TAB),
    (['[', '9', ';', '2', '~'], Keyboard.UNKNOWN),
])
def test_csi_sequence_is_consumed_before_decoding(monkeypatch, sequence, key):
    kb = Keyboard()
    chars = iter(sequence)
    monkeypatch.setattr(kb, '_read_with_timeout',
                        lambda timeout=0.05: next(chars, None))

    assert kb.read_ansi() == key
    assert next(chars, None) is None


def test_bare_escape_is_not_a_sequence(monkeypatch):
    kb = Keyboard()
    monkeypatch.setattr(kb, '_read_with_timeout', lambda timeout=0.05: None)

    assert kb.read_ansi() == Keyboard.ESCAPE


def _fake_stdin(monkeypatch, reads):
    from types import SimpleNamespace

    monkeypatch.setattr('shesha.tui.pytui.sys.stdin',
                        SimpleNamespace(fileno=lambda: 0))
    chars = iter(reads)

    def fake_read(fd, n):
        try:
            return next(chars)
        except StopIteration:
            raise AssertionError('read past EOF')

    monkeypatch.setattr('shesha.tui.pytui.os.read', fake_read)


def test_read_stops_at_stdin_eof(monkeypatch):
    """EOF must end the reader thread, not spin on an empty key."""
    kb = Keyboard()
    seen = []
    kb.add_listener(seen.append)
    _fake_stdin(monkeypatch, [b'a', b''])

    kb.read()
    assert seen == ['a']


def test_ctrl_h_is_backspace(monkeypatch):
    kb = Keyboard()
    seen = []
    kb.add_listener(seen.append)
    _fake_stdin(monkeypatch, [b'\x08', b''])

    kb.read()
    assert seen == [Keyboard.BACKSPACE]


@pytest.mark.parametrize('sequence, key', [
    (['O', 'A'], Keyboard.UP),
    (['O', 'B'], Keyboard.DOWN),
    (['O', 'C'], Keyboard.RIGHT),
    (['O', 'D'], Keyboard.LEFT),
    (['O', 'H'], Keyboard.HOME),
    (['O', 'F'], Keyboard.END),
])
def test_ss3_sequence_is_consumed_before_decoding(monkeypatch, sequence, key):
    kb = Keyboard()
    chars = iter(sequence)
    monkeypatch.setattr(kb, '_read_with_timeout',
                        lambda timeout=0.05: next(chars, None))

    assert kb.read_ansi() == key
    assert next(chars, None) is None


def test_listener_exception_is_contained_and_reported():
    kb = Keyboard()
    seen = []
    kb.on_error = seen.append

    def boom(_c):
        raise ValueError('boom')

    kb.add_listener(boom)
    kb._dispatch('x')  # must not raise
    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)
