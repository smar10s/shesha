from shesha.tui.command_model import Command, complete_buffer, resolve
from shesha.tui.fm import FmApp
from shesha.tui.wifi import WifiApp


def _root(apps):
    root = Command('')
    for app in apps:
        node = Command(app.name)
        for name, view in app.views.items():
            node.add(Command(name, complete=view.command_completions))
        node.add(*app.command_nodes(
            run=lambda name, args: None,
            completions=app.command_completions))
        root.add(node)
    return root


def test_fm_rds_lists_operations():
    app = FmApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'fm rds ', 7)
    assert {'on', 'off', 'id3', 'ps', 'rt', 'pi', 'pty'} <= set(
        comp.candidates)


def test_fm_rds_id3_lists_on_off():
    app = FmApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'fm rds id3 ', 11)
    assert comp.candidates == ['id3 on', 'id3 off']


def test_wifi_record_lists_stop_and_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'capture.pcap').write_bytes(b'')
    app = WifiApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'wifi record ', 12)
    assert 'stop' in comp.candidates
    assert any('capture.pcap' in c for c in comp.candidates)


def test_wifi_record_completes_partial_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'capture.pcap').write_bytes(b'')
    app = WifiApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'wifi record cap', 15)
    assert comp.candidates == ['capture.pcap']


def test_wifi_record_stop_resolves():
    app = WifiApp()
    root = _root([app])
    result = resolve(root, None, 'wifi record stop')
    assert result.command.name == 'stop'


def test_wifi_tx_lists_operations():
    app = WifiApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'wifi tx ', 8)
    assert {'beacon', 'frame'} <= set(comp.candidates)


def test_wifi_tx_frame_lists_frame_types():
    app = WifiApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'wifi tx frame ', 14)
    assert comp.candidates == ['beacon', 'deauth', 'raw']


def test_wifi_tx_frame_completes_partial_type():
    app = WifiApp()
    root = _root([app])

    def candidates(buffer: str) -> list[str]:
        return complete_buffer(root, None, buffer, len(buffer)).candidates

    assert candidates('wifi tx frame be') == ['beacon']
    assert candidates('wifi tx frame dea') == ['deauth']
    assert candidates('wifi tx frame ra') == ['raw']


def test_wifi_tx_completes_frame_options():
    root = _root([WifiApp()])

    def candidates(buffer: str) -> list[str]:
        return complete_buffer(root, None, buffer, len(buffer)).candidates

    assert candidates('wifi tx frame deauth --r') == ['--rate', '--reason']
    assert candidates('wifi tx frame beacon --c') == [
        '--capability', '--channel', '--count', '--cyclic']


def test_wifi_tx_options_complete_after_positional_arguments():
    root = _root([WifiApp()])
    buffer = 'wifi tx frame deauth aa:bb:cc:dd:ee:ff --s'
    comp = complete_buffer(root, None, buffer, len(buffer))
    assert comp.candidates == ['aa:bb:cc:dd:ee:ff --sa',
                               'aa:bb:cc:dd:ee:ff --seq']


def test_wifi_tx_nested_nodes_rebuild_the_full_arguments():
    calls: list[tuple[str, list[str]]] = []
    nodes = WifiApp().command_nodes(
        lambda name, args: calls.append((name, args)),
        lambda name, text: [])
    tx = next(node for node in nodes if node.name == 'tx')
    tx.children['frame'].children['deauth'].run(
        ['aa:bb:cc:dd:ee:ff', '11:22:33:44:55:66', '--reason', '7'])
    assert calls == [('tx', ['frame', 'deauth', 'aa:bb:cc:dd:ee:ff',
                             '11:22:33:44:55:66', '--reason', '7'])]


def test_wifi_command_nodes_describe_themselves():
    nodes = WifiApp().command_nodes(lambda name, args: None,
                                    lambda name, text: [])
    by_name = {node.name: node for node in nodes}
    assert by_name['tx'].description == 'Transmit WiFi frames'
    assert by_name['record'].usage == '[--eapol] [file]'
    assert by_name['record'].children['stop'].description == 'Stop recording'


def _dispatcher(app):
    from shesha.tui.commands import CommandDispatcher
    root = _root([app])
    return CommandDispatcher(root, lambda: root.child(app.name))


def test_complete_fm_tx_path(tmp_path):
    from shesha.tui.fm import FmApp
    (tmp_path / "song.mp3").touch()
    (tmp_path / "song2.wav").touch()
    d = _dispatcher(FmApp())
    partial = str(tmp_path / "son")
    comp = d.complete(f'fm tx {partial}', len(f'fm tx {partial}'))
    assert comp.start == len('fm tx ')
    assert len(comp.candidates) == 2
    assert any("song.mp3" in c for c in comp.candidates)


def test_complete_fm_tx_tilde_path(tmp_path, monkeypatch):
    from shesha.tui.fm import FmApp

    monkeypatch.setenv('HOME', str(tmp_path))
    music = tmp_path / 'Music'
    music.mkdir()
    (music / 'Hack.mp3').touch()
    d = _dispatcher(FmApp())

    buf = 'fm tx ~/Music/Hack'
    comp = d.complete(buf, len(buf))
    assert '~/Music/Hack.mp3' in comp.candidates

    buf, _cursor = d.on_tab(buf, len(buf))
    assert buf.rstrip() == 'fm tx ~/Music/Hack.mp3'


def test_tab_no_space_after_directory(tmp_path):
    from shesha.tui.fm import FmApp
    sub = tmp_path / "uniquedir"
    sub.mkdir()
    (sub / "file.mp3").touch()
    d = _dispatcher(FmApp())
    buf, cursor = d.on_tab(f'fm tx {tmp_path}/uniqu',
                           len(f'fm tx {tmp_path}/uniqu'))
    assert buf.endswith('uniquedir/')
    assert not buf.endswith('uniquedir/ ')
    buf, cursor = d.on_tab(buf, cursor)
    assert 'file.mp3' in buf


def test_fm_tx_complete_empty_lists_cwd(tmp_path, monkeypatch):
    from shesha.tui.fm import FmApp
    monkeypatch.chdir(tmp_path)
    (tmp_path / "song.mp3").touch()
    d = _dispatcher(FmApp())
    comp = d.complete('fm tx ', 6)
    assert any('song.mp3' in c for c in comp.candidates)


def test_wifi_record_completes_after_extra_spaces(tmp_path, monkeypatch):
    """Several spaces before an argument must not become part of the text."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'capture.pcap').write_bytes(b'')
    app = WifiApp()
    root = _root([app])
    comp = complete_buffer(root, None, 'wifi record  ca', 15)
    assert comp.start == len('wifi record  ')
    assert comp.candidates == ['capture.pcap']
