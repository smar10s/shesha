"""Tests for InputHandler extraction."""
from shesha.tui.input import InputHandler, InputResult, _DEFAULT_KEYS


def test_colon_enters_command_mode():
    handler = InputHandler()
    result = handler.handle_key(':')
    assert result.kind == 'enter_command_mode'
    assert handler.command_mode is True


def test_digit_accumulates():
    handler = InputHandler()
    handler.handle_key('5')
    handler.handle_key('0')
    result = handler.handle_key('f')
    assert result.kind == 'normal_action'
    assert result.key == 'f'
    assert result.count == 50
    assert result.had_digits is True


def test_escape_clears_count():
    handler = InputHandler()
    handler.handle_key('5')
    result = handler.handle_key('escape')
    assert result.kind == 'clear'
    assert handler.count_buf == ''


def test_normal_action_default_count():
    handler = InputHandler()
    result = handler.handle_key('h')
    assert result.kind == 'normal_action'
    assert result.count == 1
    assert result.had_digits is False


def test_command_mode_enter_executes():
    handler = InputHandler()
    handler.handle_key(':')
    handler.handle_key('g')
    handler.handle_key('a')
    handler.handle_key('i')
    handler.handle_key('n')
    result = handler.handle_key('\x0a')
    assert result.kind == 'execute'
    assert result.command == 'gain'
    assert handler.command_mode is False


def test_command_mode_escape_exits():
    handler = InputHandler()
    handler.handle_key(':')
    handler.handle_key('g')
    result = handler.handle_key('escape')
    assert result.kind == 'exit_command_mode'
    assert handler.command_mode is False
    assert handler.command_buf == ''


def test_command_mode_backspace():
    handler = InputHandler()
    handler.handle_key(':')
    handler.handle_key('a')
    handler.handle_key('b')
    result = handler.handle_key('\x7f')
    assert result.kind == 'backspace'
    assert handler.command_buf == 'a'


def test_command_mode_backspace_empty_exits():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('\x7f')
    assert result.kind == 'exit_on_empty_bs'
    assert handler.command_mode is False


def test_command_mode_tab():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('\x09')
    assert result.kind == 'tab'
    assert result.reverse is False


def test_command_mode_shift_tab():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('shift_tab')
    assert result.kind == 'tab'
    assert result.reverse is True


def test_command_mode_up_arrow():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('up')
    assert result.kind == 'history_back'


def test_command_mode_down_arrow():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('down')
    assert result.kind == 'history_forward'


def test_enter_on_empty_buffer():
    handler = InputHandler()
    handler.handle_key(':')
    result = handler.handle_key('\x0a')
    # Empty command → just exit
    assert result.kind == 'exit_command_mode'
    assert handler.command_mode is False


def _typed(*keys):
    handler = InputHandler()
    for key in keys:
        handler.handle_key(key)
    return handler


def test_typing_inserts_at_cursor():
    handler = _typed(':', 'a', 'c')
    handler.handle_key('left')
    handler.handle_key('b')
    assert handler.command_buf == 'abc'
    assert handler.cursor == 2


def test_backspace_deletes_before_cursor():
    handler = _typed(':', 'a', 'b', 'c')
    handler.handle_key('left')
    handler.handle_key('\x7f')
    assert handler.command_buf == 'ac'
    assert handler.cursor == 1


def test_backspace_at_start_does_nothing():
    handler = _typed(':', 'a')
    handler.handle_key('left')
    result = handler.handle_key('\x7f')
    assert result.kind == 'noop'
    assert handler.command_buf == 'a'
    assert handler.command_mode is True


def test_delete_removes_character_at_cursor():
    handler = _typed(':', 'a', 'b')
    handler.handle_key('left')
    result = handler.handle_key('delete')
    assert result.kind == 'delete'
    assert handler.command_buf == 'a'
    assert handler.cursor == 1


def test_cursor_movement_clamps():
    handler = _typed(':', 'a')
    for _ in range(3):
        handler.handle_key('left')
    assert handler.cursor == 0
    for _ in range(3):
        handler.handle_key('right')
    assert handler.cursor == 1


def test_home_end_and_ctrl_a_e():
    handler = _typed(':', 'a', 'b')
    assert handler.handle_key('home').kind == 'cursor'
    assert handler.cursor == 0
    handler.handle_key('end')
    assert handler.cursor == 2
    handler.handle_key('\x01')
    assert handler.cursor == 0
    handler.handle_key('\x05')
    assert handler.cursor == 2


def test_arrow_edit_example():
    handler = _typed(':', 'w', 'i', 'k', 'i')
    handler.handle_key('left')
    handler.handle_key('\x7f')
    handler.handle_key('f')
    assert handler.command_buf == 'wifi'
