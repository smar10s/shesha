"""Audio player selection must fit the installed ffplay.

``-ch_layout`` arrived in ffmpeg 5.1 and ``-ac`` was removed in 7.0, so
neither option works unmodified across the installed range.
"""
import io
import queue

from shesha import audio


def _skeleton_player(label: str = 'aplay') -> audio.AudioPlayer:
    """An AudioPlayer with only the fields the playhead logic touches."""
    player = audio.AudioPlayer.__new__(audio.AudioPlayer)
    player.rate = 43750
    player.label = label
    player._q = None
    player.accepted = 0
    player.dropped = 0
    player._clock_seconds = None
    return player


class _OkQueue:
    def put_nowait(self, item):
        pass


class _FullQueue:
    def put_nowait(self, item):
        raise queue.Full


def test_modern_ffplay_uses_ch_layout():
    banner = 'ffplay version 7.1.1 Copyright (c) 2003-2025'
    assert audio._ffplay_channel_args(banner) == ['-ch_layout', 'mono']
    assert audio._ffplay_channel_args('ffplay version 5.1') == ['-ch_layout', 'mono']


def test_old_ffplay_uses_ac():
    assert audio._ffplay_channel_args('ffplay version 4.4.4') == ['-ac', '1']
    assert audio._ffplay_channel_args('ffplay version 5.0.1') == ['-ac', '1']


def test_unknown_banner_falls_back_to_ac():
    assert audio._ffplay_channel_args('') == ['-ac', '1']
    assert audio._ffplay_channel_args('nonsense') == ['-ac', '1']


def test_find_player_uses_version_appropriate_flag(monkeypatch):
    monkeypatch.setattr(
        audio.shutil, 'which',
        lambda name: '/usr/bin/ffplay' if name == 'ffplay' else None)
    monkeypatch.setattr(audio, '_ffplay_version',
                        lambda: 'ffplay version 4.4.4')
    argv, label = audio.find_player(44100)
    assert label == 'ffplay'
    assert '-stats' in argv
    index = argv.index('-ac')
    assert argv[index + 1] == '1'
    assert 'mono' not in argv
    assert '-ch_layout' not in argv


def test_audio_player_does_not_inherit_the_tty(monkeypatch):
    """The player's stdout/stderr go to DEVNULL, not the terminal."""
    captured = {}

    class FakeProc:
        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            self.stdin = io.BytesIO()
            self.stderr = (io.BytesIO()
                           if kwargs.get('stderr') == audio.subprocess.PIPE
                           else None)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(
        audio.shutil, 'which',
        lambda name: '/usr/bin/ffplay' if name == 'ffplay' else None)
    monkeypatch.setattr(audio, '_ffplay_version',
                        lambda: 'ffplay version 7.1')
    monkeypatch.setattr(audio.subprocess, 'Popen', FakeProc)

    player = audio.AudioPlayer(44100)
    try:
        assert captured['stdout'] == audio.subprocess.DEVNULL
        assert captured['stderr'] == audio.subprocess.PIPE
        assert captured['stdin'] == audio.subprocess.PIPE
    finally:
        player.close()


def test_push_survives_close_between_check_and_use():
    """A close racing a push must not raise AttributeError."""
    class _Queue:
        def __init__(self):
            self.items = []

        def put_nowait(self, item):
            self.items.append(item)

    class _RacingPlayer(audio.AudioPlayer):
        def __init__(self):
            self._reads = 0
            self.real = _Queue()
            self.accepted = 0
            self.dropped = 0

        @property
        def _q(self):
            self._reads += 1
            return self.real if self._reads == 1 else None

        @_q.setter
        def _q(self, value):
            pass

    player = _RacingPlayer()
    player.push([0.5])
    assert player.real.items


def test_status_clock_parser_takes_the_latest_complete_report():
    text = ('    nan M-A:    nan fd=   0 aq=    0KB\r'
            '  -0.02 M-A:  0.000 fd=   0 aq=  172KB\r'
            '   0.07 M-A:  0.000 fd=   0 aq=  172KB\r')
    assert audio._parse_status_clock(text) == 0.07


def test_status_clock_parser_ignores_incomplete_reports():
    assert audio._parse_status_clock('   0.0') is None
    assert audio._parse_status_clock('   0.04 M-A:  0.000 fd') == 0.04


def test_push_reports_acceptance_and_counts_only_accepted_samples():
    player = _skeleton_player()
    player._q = _OkQueue()
    assert player.push([0.5, -0.5]) is True
    assert player.accepted == 2

    player._q = _FullQueue()
    assert player.push([1.0]) is False
    assert player.accepted == 2
    assert player.dropped == 1


def test_fixed_delay_playhead_for_players_without_a_clock():
    player = _skeleton_player(label='aplay')
    player.accepted = 87_500
    assert player.played_samples() == (
        87_500 - int(audio._FALLBACK_DELAY_SEC * player.rate))


def test_ffplay_without_a_clock_yet_has_no_playhead():
    player = _skeleton_player(label='ffplay')
    player.accepted = 10_000
    assert player.played_samples() is None


def test_ffplay_playhead_comes_from_the_status_clock():
    player = _skeleton_player(label='ffplay')
    player._clock_seconds = 0.5
    assert player.played_samples() == 21_875
