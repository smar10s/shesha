"""Tests for command dispatch, history, and tree completion."""
from shesha.tui.command_model import Command
from shesha.tui.commands import CommandDispatcher


def _dispatcher():
    calls = []
    root = Command('')
    app = Command('wifi', run=lambda a: calls.append(('wifi', a)))
    app.add(Command('scan', run=lambda a: calls.append(('scan', a))),
            Command('record', run=lambda a: calls.append(('record', a)),
                    children={'stop': Command(
                        'stop', run=lambda a: calls.append(('stop', a)))}))
    root.add(Command('quit', run=lambda a: calls.append(('quit', a)),
                     aliases=('q',)),
             Command('set', run=lambda a: calls.append(('set', a)),
                     complete=lambda text: ['freq', 'gain']),
             app)
    return CommandDispatcher(root, lambda: app), calls


def test_resolve_global_command():
    d, _ = _dispatcher()
    result = d.resolve('quit')
    assert result.command.name == 'quit'
    assert result.args == []


def test_resolve_app_child_with_explicit_app():
    d, _ = _dispatcher()
    result = d.resolve('wifi record stop')
    assert result.command.name == 'stop'


def test_resolve_active_app_child():
    d, _ = _dispatcher()
    result = d.resolve('record')
    assert result.command.name == 'record'


def test_resolve_unknown_returns_none():
    d, _ = _dispatcher()
    assert d.resolve('nope') is None


def test_complete_uses_tree():
    d, _ = _dispatcher()
    assert 'record' in d.complete('re', 2).candidates
    assert 'stop' in d.complete('wifi record ', 12).candidates


def test_complete_arguments():
    d, _ = _dispatcher()
    assert d.complete('set ', 4).candidates == ['freq', 'gain']


def test_tab_returns_buffer_and_cursor():
    d, _ = _dispatcher()
    buf, cursor = d.on_tab('qui', 3)
    assert buf == 'quit'
    assert cursor == 4


def test_history_push_dedup():
    d, _ = _dispatcher()
    d.push_history('gain 50')
    d.push_history('gain 50')
    assert len(d._command_history) == 1


def test_history_navigation():
    d, _ = _dispatcher()
    d.push_history('gain 50')
    d.push_history('freq 5200')
    buf = d.history_back('')
    assert buf == 'freq 5200'
    buf = d.history_back(buf)
    assert buf == 'gain 50'
    buf = d.history_forward(buf)
    assert buf == 'freq 5200'
    buf = d.history_forward(buf)
    assert buf == ''


# ─── Filesystem path completion ─────────────────────────────────────────


def test_complete_file_path(tmp_path):
    """Tab completing a partial path returns matching files."""
    from shesha.tui.completion import complete_path

    (tmp_path / "song.mp3").touch()
    (tmp_path / "song2.wav").touch()
    (tmp_path / "other.txt").touch()
    results = complete_path(str(tmp_path / "son"))
    assert len(results) == 2
    assert any("song.mp3" in r for r in results)


def test_complete_path_directory(tmp_path):
    """Tab completing a directory returns its contents."""
    from shesha.tui.completion import complete_path

    sub = tmp_path / "music"
    sub.mkdir()
    (sub / "track.mp3").touch()
    results = complete_path(str(tmp_path / "music/"))
    assert len(results) == 1


def test_complete_path_dirs_get_trailing_slash(tmp_path):
    """Directory entries include a trailing separator."""
    from shesha.tui.completion import complete_path

    sub = tmp_path / "subdir"
    sub.mkdir()
    (tmp_path / "subfile.txt").touch()
    results = complete_path(str(tmp_path / "sub"))
    dir_matches = [r for r in results if r.endswith('/')]
    assert len(dir_matches) == 1


def test_complete_path_empty_returns_cwd(tmp_path, monkeypatch):
    """Empty partial returns current directory listing."""
    from shesha.tui.completion import complete_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "file.txt").touch()
    (tmp_path / "subdir").mkdir()
    results = complete_path('')
    assert len(results) >= 2
    assert any("file.txt" in r for r in results)


def test_is_directory_completion():
    from shesha.tui.completion import is_directory_completion

    assert is_directory_completion('/tmp/foo/')
    assert not is_directory_completion('/tmp/foo.txt')


def test_complete_path_hidden_sorted_last(tmp_path):
    """Dot-prefixed entries sort after visible entries."""
    from shesha.tui.completion import complete_path
    import os

    (tmp_path / ".hidden").touch()
    (tmp_path / "alpha.txt").touch()
    (tmp_path / "zebra.txt").touch()
    results = complete_path(str(tmp_path) + "/")
    names = [os.path.basename(r.rstrip("/")) for r in results]
    assert names.index(".hidden") > names.index("zebra.txt")


def test_complete_path_keeps_tilde_prefix(tmp_path, monkeypatch):
    """Candidates keep the ~ the user typed, so they match the partial."""
    from shesha.tui.completion import complete_path

    monkeypatch.setenv('HOME', str(tmp_path))
    music = tmp_path / 'Music'
    music.mkdir()
    (music / 'Hacking').mkdir()
    (music / 'Hack.mp3').touch()

    assert complete_path('~') == ['~/']
    assert complete_path('~/Mus') == ['~/Music/']
    assert complete_path('~/Music/Hack') == [
        '~/Music/Hack.mp3', '~/Music/Hacking/']
    assert complete_path('~/Music/Hacking/') == []


def test_complete_path_keeps_tilde_user_prefix(tmp_path, monkeypatch):
    """A named home keeps its ~user form too."""
    from shesha.tui import completion
    from shesha.tui.completion import complete_path

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(completion.os.path, 'expanduser',
                        lambda p: str(tmp_path) if p == '~alice' else p)
    (tmp_path / 'song.mp3').touch()

    assert complete_path('~alice/son') == ['~alice/song.mp3']


# ─── Path expansion (execute-time) ──────────────────────────────────────


def test_has_glob():
    from shesha.tui.completion import has_glob

    assert has_glob('/a/*.mp3')
    assert has_glob('/a/track?.mp3')
    assert has_glob('/a/[abc].mp3')
    assert not has_glob('/a/b/track.mp3')


def test_expand_paths_directory_lists_files_only(tmp_path):
    """A directory expands to its immediate, non-hidden files."""
    import os

    from shesha.tui.completion import expand_paths

    (tmp_path / 'a.mp3').touch()
    (tmp_path / 'b.wav').touch()
    (tmp_path / 'notes.txt').touch()
    (tmp_path / '.hidden.mp3').touch()
    (tmp_path / 'sub').mkdir()

    results = expand_paths(str(tmp_path) + '/')
    assert [os.path.basename(r) for r in results] == ['a.mp3', 'b.wav',
                                                      'notes.txt']


def test_expand_paths_bare_directory_without_slash(tmp_path):
    import os

    from shesha.tui.completion import expand_paths

    (tmp_path / 'a.mp3').touch()
    results = expand_paths(str(tmp_path))
    assert [os.path.basename(r) for r in results] == ['a.mp3']


def test_expand_paths_glob(tmp_path):
    import os

    from shesha.tui.completion import expand_paths

    (tmp_path / 'a.mp3').touch()
    (tmp_path / 'b.mp3').touch()
    (tmp_path / 'c.wav').touch()

    results = expand_paths(str(tmp_path) + '/*.mp3')
    assert [os.path.basename(r) for r in results] == ['a.mp3', 'b.mp3']


def test_expand_paths_glob_excludes_directories(tmp_path):
    import os

    from shesha.tui.completion import expand_paths

    (tmp_path / 'a.mp3').touch()
    (tmp_path / 'sub.mp3').mkdir()
    results = expand_paths(str(tmp_path) + '/*.mp3')
    assert [os.path.basename(r) for r in results] == ['a.mp3']


def test_expand_paths_glob_no_match(tmp_path):
    from shesha.tui.completion import expand_paths

    assert expand_paths(str(tmp_path) + '/*.mp3') == []


def test_expand_paths_plain_file(tmp_path):
    from shesha.tui.completion import expand_paths

    song = tmp_path / 'song.mp3'
    song.touch()
    assert expand_paths(str(song)) == [str(song)]


def test_expand_paths_missing_returns_empty(tmp_path):
    from shesha.tui.completion import expand_paths

    assert expand_paths(str(tmp_path / 'gone.mp3')) == []


def test_expand_paths_expanduser(tmp_path, monkeypatch):
    from shesha.tui.completion import expand_paths

    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'song.mp3').touch()
    assert expand_paths('~/song.mp3') == [str(tmp_path / 'song.mp3')]


# ─── Completion engine ──────────────────────────────────────────────────


def test_tokenize_spans():
    from shesha.tui.completion import tokenize

    toks = tokenize('fm tx /a/b c')
    assert toks == [('fm', 0, 2), ('tx', 3, 5), ('/a/b', 6, 10), ('c', 11, 12)]
    assert tokenize('') == []


def test_engine_cycles_candidates_with_spaces():
    from shesha.tui.completion import Completion, CompletionEngine

    cands = ['/a/Census - Thank you.pdf', '/a/Die Drei Fragezeichen/']
    engine = CompletionEngine(is_drillable=lambda s: s.endswith('/'))

    def complete_fn(buf, _cursor):
        return Completion(6, len(buf), cands)

    buf = 'fm tx /a/'
    buf, cur = engine.on_tab(buf, len(buf), complete_fn)
    assert buf == 'fm tx /a/Census - Thank you.pdf'
    buf, cur = engine.on_tab(buf, cur, complete_fn)
    assert buf == 'fm tx /a/Die Drei Fragezeichen/'
    buf, cur = engine.on_tab(buf, cur, complete_fn)
    assert buf == 'fm tx /a/Census - Thank you.pdf'


def test_engine_unique_match_appends_space_when_more_follow():
    from shesha.tui.completion import Completion, CompletionEngine

    def complete_fn(buf, _cursor):
        if buf.endswith('ga'):
            return Completion(4, len(buf), ['gain'])
        return Completion(4, len(buf), ['fast', 'slow', 'auto'])

    engine = CompletionEngine()
    assert engine.on_tab('set ga', 6, complete_fn) == ('set gain ', 9)


def test_engine_unique_directory_skips_space():
    from shesha.tui.completion import Completion, CompletionEngine

    def complete_fn(buf, _cursor):
        return Completion(3, len(buf), ['Projects/'])

    engine = CompletionEngine(is_drillable=lambda s: s.endswith('/'))
    assert engine.on_tab('cd Pro', 6, complete_fn) == ('cd Projects/', 12)


def test_engine_reverse_starts_at_last_candidate():
    from shesha.tui.completion import Completion, CompletionEngine

    def complete_fn(buf, _cursor):
        return Completion(0, len(buf), ['a', 'b', 'c'])

    engine = CompletionEngine()
    assert engine.on_tab('', 0, complete_fn, reverse=True) == ('c', 1)
