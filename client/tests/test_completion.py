from shesha.tui.completion import (
    Completion,
    CompletionEngine,
    common_prefix,
    token_at,
)


def test_common_prefix():
    assert common_prefix(['alpha', 'alpine']) == 'alp'
    assert common_prefix(['one']) == 'one'
    assert common_prefix([]) == ''


def test_token_at_end_of_buffer():
    assert token_at('wifi', 4) == (0, 4)
    assert token_at('wifi ', 5) == (5, 5)


def test_token_at_inside_token():
    assert token_at('wifi rest', 2) == (0, 4)
    assert token_at('wifi rest', 7) == (5, 9)


def test_tab_extends_shared_prefix_then_cycles():
    engine = CompletionEngine()

    def complete(_buf, _cur):
        return Completion(0, 2, ['alpha', 'alpine'])

    buf, cur = engine.on_tab('al', 2, complete)
    assert (buf, cur) == ('alp', 3)
    buf, cur = engine.on_tab(buf, cur, complete)
    assert (buf, cur) == ('alpha', 5)
    buf, cur = engine.on_tab(buf, cur, complete)
    assert (buf, cur) == ('alpine', 6)


def test_tab_single_candidate_appends_space_when_more_args():
    engine = CompletionEngine()

    def complete(buf, cur):
        if buf == 'fm':
            return Completion(0, 2, ['fm'])
        if buf == 'fm ':
            return Completion(3, 3, ['tx'])
        return Completion(cur, cur, [])

    buf, cur = engine.on_tab('fm', 2, complete)
    assert (buf, cur) == ('fm ', 3)


def test_tab_replaces_token_and_keeps_suffix():
    engine = CompletionEngine()

    def complete(buf, cur):
        if buf.startswith('wi'):
            return Completion(0, 2, ['wifi'])
        return Completion(cur, cur, [])

    buf, cur = engine.on_tab('wi rest', 2, complete)
    assert buf == 'wifi rest'
    assert cur == 4


def test_tab_without_matches_is_noop():
    engine = CompletionEngine()

    def complete(_buf, _cur):
        return Completion(0, 1, [])

    assert engine.on_tab('x', 1, complete) == ('x', 1)
