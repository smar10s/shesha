from shesha.tui.command_model import (
    Command,
    complete_buffer,
    resolve,
)


def _tree():
    calls = []

    def run(name):
        return lambda args: calls.append((name, args))

    root = Command('')
    app = Command('wifi', run=run('wifi'))
    app.add(
        Command('scan', run=run('scan')),
        Command('record', run=run('record'),
                complete=lambda text: ['capture.pcap'],
                children={'stop': Command('stop', run=run('stop'))}),
    )
    other = Command('fm', run=run('fm'))
    other.add(Command('rds', run=run('rds'),
                      complete=lambda text: [
                          op for op in ('on', 'off', 'id3')
                          if op.startswith(text)]))
    other.add(Command('tx', run=run('tx'),
                      complete=lambda text: [
                          path for path in ['/a/Census - Thank you.mp3']
                          if path.startswith(text)]))
    root.add(app, other, Command('stop_tx', run=run('stop_tx'),
                                 aliases=('kill',)))
    return root, app, calls


def test_resolve_global_first():
    root, app, _ = _tree()
    assert resolve(root, app, 'stop_tx').command.name == 'stop_tx'
    assert resolve(root, app, 'wifi').command.name == 'wifi'


def test_resolve_descends_into_app():
    root, app, _ = _tree()
    result = resolve(root, app, 'wifi record stop')
    assert result.command.name == 'stop'
    assert result.args == []


def test_resolve_stops_at_first_argument():
    root, app, _ = _tree()
    result = resolve(root, app, 'wifi record capture.pcap')
    assert result.command.name == 'record'
    assert result.args == ['capture.pcap']


def test_resolve_active_app_fallback():
    root, app, _ = _tree()
    result = resolve(root, app, 'record stop')
    assert result.command.name == 'stop'
    assert result.args == []


def test_resolve_unknown_returns_none():
    root, app, _ = _tree()
    assert resolve(root, app, 'nope') is None


def test_resolve_alias():
    root, app, _ = _tree()
    assert resolve(root, app, 'kill').command.name == 'stop_tx'


def test_complete_first_token_merges_global_and_active():
    root, app, _ = _tree()
    names = complete_buffer(root, app, 're', 2).candidates
    assert 'record' in names


def test_complete_child_names():
    root, app, _ = _tree()
    comp = complete_buffer(root, app, 'wifi ', 5)
    assert {'scan', 'record'} <= set(comp.candidates)


def test_complete_argument_handler():
    root, app, _ = _tree()
    comp = complete_buffer(root, app, 'wifi record ', 12)
    assert 'stop' in comp.candidates
    assert 'capture.pcap' in comp.candidates


def test_complete_argument_after_nested_arg():
    root, app, _ = _tree()
    comp = complete_buffer(root, app, 'fm rds i', 8)
    assert comp.candidates == ['id3']


def test_complete_argument_owns_the_rest_of_the_line():
    root, app, _ = _tree()
    comp = complete_buffer(root, app, 'fm tx /a/Census - Th', 20)
    assert comp.start == 6
    assert comp.end == 20
    assert comp.candidates == ['/a/Census - Thank you.mp3']


def test_complete_replaces_mid_line_token():
    root, app, _ = _tree()
    comp = complete_buffer(root, app, 'wi rest', 2)
    assert comp.start == 0
    assert comp.end == 2
    assert 'wifi' in comp.candidates
