"""Tests for the FM TUI app (dial rendering, RX view state, dummy mode)."""
import pytest

from shesha.tui.fm import (
    Band, BAND_FM, BAND_AM, render_dial, dial_window,
    FREQ_MIN_HZ, FREQ_MAX_HZ, FmRxView, FmTxView, FmApp,
    _pad, _render_dial_styled,
)
from shesha.tui.dummy import DummyDaemon
from shesha.tui.config import Config


class _FakePlayer:
    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.pushes: list[list[float]] = []
        self.closed = False
        self.player_gone = False
        self.played = None

    def push(self, audio: list[float]) -> bool:
        self.pushes.append(list(audio))
        return True

    def played_samples(self):
        return self.played

    def close(self) -> None:
        self.closed = True


# ─── Band ────────────────────────────────────────────────────────────


def test_band_labels():
    assert BAND_FM.label(90_000_000) == '90'
    assert BAND_FM.label(100_000_000) == '100'
    assert BAND_FM.label(105_000_000) == '105'
    assert BAND_AM.label(600_000) == '600'
    assert BAND_AM.label(1_000_000) == '1000'


def test_bands_have_steps():
    assert BAND_FM.fine_hz == 100_000
    assert BAND_AM.fine_hz == 10_000


# ─── Dial rendering ───────────────────────────────────────────────────


def test_render_dial_caret_at_edges():
    w = 40
    ticks, caret, labels = render_dial(
        w, BAND_FM.lo_hz, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert len(ticks) == w and len(caret) == w and len(labels) == w
    assert caret.count('^') == 1
    assert caret[0] == '^'

    ticks, caret, labels = render_dial(
        w, BAND_FM.hi_hz, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert caret.count('^') == 1
    assert caret[w - 1] == '^'


def test_render_dial_caret_mid_band():
    w = 40
    mid = (BAND_FM.lo_hz + BAND_FM.hi_hz) // 2
    _ticks, caret, _labels = render_dial(
        w, mid, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert caret.find('^') in (w // 2 - 1, w // 2, w // 2 + 1)


def test_render_dial_ticks_and_labels():
    w = 80
    ticks, caret, labels = render_dial(
        w, 100_100_000, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert '|' in ticks
    assert '^' in caret
    assert '90' in labels
    assert '100' in labels
    assert '105' in labels


def test_render_dial_outside_band_shifted_window():
    """Out-of-band freq gets a shifted window; caret tracks exactly."""
    w = 80
    freq = 162_000_000
    lo, hi, major, label_every = dial_window(freq, BAND_FM)
    ticks, caret, labels = render_dial(
        w, freq, BAND_FM, lo_hz=lo, hi_hz=hi,
        major_hz=major, label_every_hz=label_every)
    # Caret should be roughly centered (not clamped to an edge)
    pos = caret.find('^')
    assert w // 4 < pos < 3 * w // 4


def test_render_dial_freq_label_on_spacious_side():
    w = 60
    # Left edge → caret at column 0, label to its right.
    _ticks, caret, _labels = render_dial(
        w, BAND_FM.lo_hz, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert caret.startswith('^ 87.5 MHz')
    assert caret.count('^') == 1

    # Right edge → caret at last column, label to its left.
    _ticks, caret, _labels = render_dial(
        w, BAND_FM.hi_hz, BAND_FM,
        lo_hz=BAND_FM.lo_hz, hi_hz=BAND_FM.hi_hz)
    assert caret.rstrip().endswith('^')
    assert '108 MHz' in caret
    assert caret.count('^') == 1


# ─── dial_window() ───────────────────────────────────────────────────


def test_dial_window_inside_band():
    """Freq inside FM band returns the band's own lo/hi and tick spacing."""
    lo, hi, major, label_every = dial_window(100_100_000, BAND_FM)
    assert lo == BAND_FM.lo_hz
    assert hi == BAND_FM.hi_hz
    assert major == BAND_FM.major_hz
    assert label_every == BAND_FM.label_every_hz


def test_dial_window_outside_band():
    """Freq at 162.5 MHz returns a 10 MHz window centered there."""
    lo, hi, major, label_every = dial_window(162_500_000, BAND_FM)
    assert lo == 157_500_000
    assert hi == 167_500_000
    assert major == 1_000_000
    assert label_every == 5_000_000


def test_dial_window_clamps_to_ad9361_range():
    """Window near 70 MHz floor does not go below FREQ_MIN_HZ."""
    lo, hi, major, label_every = dial_window(72_000_000, BAND_FM)
    assert lo >= FREQ_MIN_HZ
    # Window shifts right so it doesn't dip below the hardware floor
    assert hi - lo == 10_000_000


def test_render_dial_outside_band():
    """Freq at 162 MHz renders with caret centered and 1 MHz ticks."""
    w = 80
    freq = 162_000_000
    lo, hi, major, label_every = dial_window(freq, BAND_FM)
    ticks, caret, labels = render_dial(
        w, freq, BAND_FM, lo_hz=lo, hi_hz=hi,
        major_hz=major, label_every_hz=label_every)
    pos = caret.find('^')
    # Caret should be roughly centered
    assert w // 4 < pos < 3 * w // 4
    # Ticks should exist at 1 MHz intervals
    assert ticks.count('|') >= 8  # 10 MHz window / 1 MHz = ~10 ticks


def test_fine_tune_past_band_edge(tmp_path):
    """Fine-tune from 107.9 to 108.1 MHz works (does not clamp to 108)."""
    view = FmRxView(player_factory=None)
    view.config = Config(str(tmp_path / 'c.json'))
    view._freq_hz = 107_900_000
    view._fine_tune(2)  # +200 kHz = 108.1 MHz
    assert view._freq_hz == 108_100_000
    assert view.config.get('freq') == '108100000'


# ─── FmRxView ─────────────────────────────────────────────────────────


def test_fm_rx_dial_has_no_fake_frequency_before_state():
    from shesha.tui.pytui import StyledWindow, Text

    view = FmRxView(player_factory=None)
    view.layout(StyledWindow(0, 0, 80, 12))
    view._render_dial()
    blob = '\n'.join(Text(line).strip_ansi()
                     for line in view._dial_win.buffer)
    assert '100.1 MHz' not in blob
    assert '^' not in blob
    assert '--' in blob


def test_fm_rx_fine_tune_waits_for_a_known_frequency(tmp_path):
    view = FmRxView(player_factory=None)
    view.config = Config(str(tmp_path / 'c.json'))
    view._fine_tune(1)
    assert view._freq_hz is None


def test_fm_rx_update_pushes_audio():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 43750,
                 'audio': [0.1, 0.2, -0.3]})
    assert view._audio_rate == 43750
    assert list(view._buf) == [0.1, 0.2, -0.3]
    assert view._player is not None
    assert view._player.pushes[-1] == [0.1, 0.2, -0.3]


def test_fm_rx_waveform_follows_the_playhead_not_the_newest_samples():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 1000,
                 'audio': list(range(10))})
    view._player.played = 5

    assert view._waveform_samples() == [0, 1, 2, 3, 4]


def test_fm_rx_waveform_keeps_history_while_the_playhead_lags():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 1000, 'audio': list(range(10))})
    view._player.played = 5
    assert view._waveform_samples() == [0, 1, 2, 3, 4]

    view.update({'type': 'fm', 'audio_rate': 1000,
                 'audio': list(range(10, 15))})
    assert view._waveform_samples() == [0, 1, 2, 3, 4]


def test_fm_rx_waveform_shows_newest_samples_without_a_playhead():
    view = FmRxView(player_factory=None)
    view.update({'type': 'fm', 'audio_rate': 1000,
                 'audio': [1.0, 2.0, 3.0]})
    assert view._waveform_samples() == [1.0, 2.0, 3.0]


def test_fm_rx_dropped_audio_is_not_drawn():
    class _DroppingPlayer(_FakePlayer):
        def push(self, audio):
            self.pushes.append(list(audio))
            return False

    view = FmRxView(player_factory=_DroppingPlayer)
    view.update({'type': 'fm', 'audio_rate': 1000, 'audio': [0.5]})
    assert list(view._buf) == []


def test_fm_rx_player_restart_resets_the_waveform_timeline():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 1000, 'audio': [0.1, 0.2]})
    first = view._player
    first.player_gone = True

    view.update({'type': 'fm', 'audio_rate': 1000, 'audio': [0.3]})

    assert view._player is not first
    assert list(view._buf) == [0.3]
    assert view._accepted == 1


def _rds_state():
    from shesha.protocol import RdsState
    return RdsState(
        synced=True, pi_valid=True, pi=0x8203, pty=10,
        tp=True, ta=False, ms=True, di=0,
        ps='SHESHA  ', ps_mask=0xFF,
        rt='hello'.ljust(64), rt_len=5, rt_mask=0x1F,
        rt_ab=False, rt_valid=True,
        ptyn='TEST', ptyn_mask=0xFF, ptyn_ab=False,
        af=[], ct_mjd=0, ct_minutes=0, ct_offset_half_hours=0,
        ct_valid=False, oda=[], groups=0, blocks=0,
        block_errors=0, sync_losses=0,
    )


def test_fm_rx_update_stores_rds():
    view = FmRxView(player_factory=None)
    state = _rds_state()
    view.update({'type': 'fm', 'audio_rate': 43750,
                 'audio': [0.1], 'rds': state})
    assert view._rds is state
    assert view._rds_dirty


def _padded_pty0_state():
    """What 90.1 sends: centred padded PS, PTY 0 and no PTYN."""
    from shesha.protocol import RdsState
    rt = 'Northwest Public Broadcasting'
    return RdsState(
        synced=True, pi_valid=True, pi=0x349F, pty=0,
        tp=False, ta=False, ms=True, di=0,
        ps='  NWPB  ', ps_mask=0xFF,
        rt=rt.ljust(64), rt_len=len(rt), rt_mask=(1 << len(rt)) - 1,
        rt_ab=False, rt_valid=True,
        ptyn='        ', ptyn_mask=0, ptyn_ab=False,
        af=[], ct_mjd=0, ct_minutes=0, ct_offset_half_hours=0,
        ct_valid=False, oda=[], groups=0, blocks=0,
        block_errors=0, sync_losses=0,
    )


def test_fm_rx_rds_trims_padding_and_blanks_pty_zero():
    from shesha.tui.pytui import StyledWindow, Text
    view = FmRxView(player_factory=None)
    view.layout(StyledWindow(0, 0, 80, 12))
    view._rds = _padded_pty0_state()
    view._render_rds()
    lines = [Text(line).strip_ansi() for line in view._rds_win.buffer]
    joined = '\n'.join(lines)

    station = next(line for line in lines if 'Station:' in line)
    assert station[12:16] == 'NWPB'
    program = next(line for line in lines if 'Program:' in line)
    assert program[12:15] == '---'
    pty = next(line for line in lines if 'PTY / PI:' in line)
    assert pty[12:16] == '349F'
    assert 'None' not in joined


def test_fm_rx_update_ignores_non_fm():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'psd', 'bins': [1.0]})
    assert view._player is None
    assert len(view._buf) == 0


def test_fm_rx_update_without_player_factory():
    view = FmRxView(player_factory=None)
    view.update({'type': 'fm', 'audio_rate': 43750, 'audio': [0.5]})
    assert view._player is None
    assert list(view._buf) == [0.5]


def test_fm_rx_rate_change_restarts_player(tmp_path):
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 43750, 'audio': [0.1]})
    first = view._player
    view.update({'type': 'fm', 'audio_rate': 48000, 'audio': [0.2]})
    assert view._audio_rate == 48000
    assert first.closed
    assert view._player is not first


def test_fm_rx_update_recreates_a_dead_player():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 43750, 'audio': [0.1]})
    first = view._player
    first.player_gone = True

    view.update({'type': 'fm', 'audio_rate': 43750, 'audio': [0.2]})

    assert first.closed
    assert view._player is not first
    assert view._player.pushes[-1] == [0.2]


def test_fm_rx_on_exit_closes_player():
    view = FmRxView(player_factory=_FakePlayer)
    view.update({'type': 'fm', 'audio_rate': 43750, 'audio': [0.1, 0.2]})
    p = view._player
    view.on_exit()
    assert p.closed
    assert view._player is None


def test_fm_rx_fine_tune(tmp_path):
    view = FmRxView(player_factory=None)
    view.config = Config(str(tmp_path / 'c.json'))
    view._freq_hz = 100_100_000
    view._fine_tune(2)
    assert view.config.get('freq') == '100300000'


def test_fm_rx_fine_tune_clamps_to_ad9361_range(tmp_path):
    view = FmRxView(player_factory=None)
    view.config = Config(str(tmp_path / 'c.json'))
    view._freq_hz = FREQ_MAX_HZ
    view._fine_tune(1)
    assert view._freq_hz == FREQ_MAX_HZ
    assert view.config.get('freq') == str(FREQ_MAX_HZ)


def test_fm_rx_config_changed_tracks_freq():
    view = FmRxView(player_factory=None)

    class D:
        def get_state(self):
            return {'freq': '96500000'}

    view.config_changed('freq', D())
    assert view._freq_hz == 96_500_000


def test_fm_rx_bare_decimal_is_mhz():
    """The command hint advertises ``100.1``; it must mean MHz."""
    view = FmRxView(player_factory=None)
    applied = []

    class D:
        def apply_config(self, key, value):
            applied.append((key, value))

    view.daemon = D()
    assert view.command(['100.1'])[0] is None
    assert view._freq_hz == 100_100_000
    assert applied == [('freq', '100100000')]


def test_fm_rx_out_of_range_leaves_the_tuning_alone():
    view = FmRxView(player_factory=None)
    applied = []

    class D:
        def apply_config(self, key, value):
            applied.append((key, value))

    view.daemon = D()
    view._freq_hz = 100_100_000
    error, _ = view.command(['10'])
    assert error is not None
    assert view._freq_hz == 100_100_000
    assert applied == []


# ─── FmApp / DummyDaemon ──────────────────────────────────────────────


def test_fm_views_declare_fm_mode():
    assert FmRxView.mode == 3
    assert FmTxView.mode == 3


def test_fm_app_default_view_is_rx():
    app = FmApp()
    assert app.default_view == 'rx'
    assert 'rx' in app.views


def test_dummy_fm_poll_emits_audio(tmp_path):
    daemon = DummyDaemon(Config(str(tmp_path / 'c.json')))
    daemon.set_mode(3)
    frames = daemon.poll()
    assert len(frames) == 1
    frame = frames[0]
    assert frame['type'] == 'fm'
    assert frame['audio_rate'] == 43750
    assert len(frame['audio']) == 1024
    rds = frame['rds']
    assert rds.synced and rds.pi == 0x8203
    assert rds.ps_complete and rds.rt_valid


# ─── FmTxView ─────────────────────────────────────────────────────────


def test_fm_tx_view_initial_state():
    """TX view starts in IDLE state."""
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    assert view._state == 'idle'


def test_fm_tx_view_keybinds():
    """TX view uses Enter to play and Space to stop; q stays unshadowed."""
    from shesha.tui.fm import FmTxView
    from shesha.tui.pytui import Keyboard
    view = FmTxView()
    keys = view.keybinds()
    assert '[' in keys
    assert ']' in keys
    assert Keyboard.ENTER in keys
    assert ' ' in keys
    for nav in ('j', 'k', 'up', 'down', 'home', 'end'):
        assert nav in keys
    assert 'q' not in keys


def test_fm_tx_command_queues_instead_of_playing(tmp_path):
    """`:fm tx <path>` appends to the playlist and does not start TX."""
    from shesha.tui.fm import FmTxView
    one = tmp_path / 'one.mp3'
    two = tmp_path / 'two words.mp3'
    one.write_bytes(b'x')
    two.write_bytes(b'x')
    view = FmTxView()
    view.command([str(one)])
    view.command([str(two)])
    assert [t.path for t in view._playlist] == [str(one), str(two)]
    assert view._selected == 1
    assert view._state == 'idle'
    assert view._playing is None


def test_fm_tx_command_rejects_non_audio(tmp_path):
    """A non-audio file never enters the playlist."""
    from shesha.tui.fm import FmTxView
    notes = tmp_path / 'notes.txt'
    notes.write_text('hello')
    view = FmTxView()
    view.command([str(notes)])
    assert view._playlist == []
    assert 'not an audio file' in view._status_msg


def test_fm_tx_command_rejects_missing_file(tmp_path):
    """A missing path never enters the playlist."""
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.command([str(tmp_path / 'gone.mp3')])
    assert view._playlist == []
    assert 'not a file' in view._status_msg


def test_fm_tx_command_expands_tilde_file(tmp_path, monkeypatch):
    """A leading ~ resolves against HOME for a single file."""
    from shesha.tui.fm import FmTxView
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'song.mp3').write_bytes(b'x')
    view = FmTxView()
    err, _detail = view.command(['~/song.mp3'])
    assert err is None
    assert [t.path for t in view._playlist] == [str(tmp_path / 'song.mp3')]


def test_fm_tx_command_expands_directory_to_audio(tmp_path):
    """`:fm tx <dir>/` queues every audio file, skipping the rest."""
    from shesha.tui.fm import FmTxView
    (tmp_path / 'a.mp3').write_bytes(b'x')
    (tmp_path / 'b.flac').write_bytes(b'x')
    (tmp_path / 'notes.txt').write_text('x')
    (tmp_path / 'sub').mkdir()
    view = FmTxView()
    view.command([str(tmp_path) + '/'])
    assert [t.name for t in view._playlist] == ['a.mp3', 'b.flac']
    assert view._selected == 0
    assert view._state == 'idle'
    assert '2 tracks' in view._status_msg


def test_fm_tx_command_expands_glob(tmp_path):
    """`:fm tx <dir>/*.mp3` queues the matching files."""
    from shesha.tui.fm import FmTxView
    (tmp_path / 'a.mp3').write_bytes(b'x')
    (tmp_path / 'b.mp3').write_bytes(b'x')
    (tmp_path / 'c.wav').write_bytes(b'x')
    view = FmTxView()
    view.command([str(tmp_path) + '/*.mp3'])
    assert [t.name for t in view._playlist] == ['a.mp3', 'b.mp3']
    assert view._selected == 0


def test_fm_tx_command_glob_no_match_reports(tmp_path):
    """A wildcard matching nothing queues nothing and says so."""
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.command([str(tmp_path) + '/*.mp3'])
    assert view._playlist == []
    assert 'no files match' in view._status_msg


def test_fm_tx_command_directory_without_audio_reports(tmp_path):
    """A directory with no audio files queues nothing and says so."""
    from shesha.tui.fm import FmTxView
    (tmp_path / 'notes.txt').write_text('x')
    view = FmTxView()
    view.command([str(tmp_path) + '/'])
    assert view._playlist == []
    assert 'no audio files' in view._status_msg


def test_fm_tx_command_glob_filters_audio(tmp_path):
    """A broad wildcard keeps only the audio files."""
    from shesha.tui.fm import FmTxView
    (tmp_path / 'a.mp3').write_bytes(b'x')
    (tmp_path / 'notes.txt').write_text('x')
    view = FmTxView()
    view.command([str(tmp_path) + '/*'])
    assert [t.name for t in view._playlist] == ['a.mp3']


def test_fm_tx_command_batch_selects_first_new(tmp_path):
    """Queueing a batch selects its first track, not the last."""
    from shesha.tui.fm import FmTxView
    (tmp_path / 'solo.mp3').write_bytes(b'x')
    album = tmp_path / 'album'
    album.mkdir()
    (album / 'a.mp3').write_bytes(b'x')
    (album / 'b.mp3').write_bytes(b'x')
    view = FmTxView()
    view.command([str(tmp_path / 'solo.mp3')])
    view.command([str(album) + '/'])
    assert [t.name for t in view._playlist] == ['solo.mp3', 'a.mp3', 'b.mp3']
    assert view._selected == 1


def test_fm_tx_enter_on_empty_playlist_is_a_noop():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._enter()
    assert view._state == 'idle'


def test_fm_tx_selection_clamps(tmp_path):
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    for name in ('a.mp3', 'b.mp3'):
        (tmp_path / name).write_bytes(b'x')
        view.command([str(tmp_path / name)])
    view._select_more(-5)
    assert view._selected == 0
    view._select_more(99)
    assert view._selected == 1


class _StreamDaemon:
    """Minimal daemon: accepts a stream start and reports idle telemetry."""

    def fm_tx_stream_start(self, audio, *, audio_rate=44100, **kw):
        return True

    def fm_tx_stream_push(self, audio):
        from shesha.client import PushResult
        return PushResult.OK

    def fm_tx_stream_end(self):
        return True

    def tx_stop(self):
        pass

    def get_state(self):
        return {'tx_busy': '1', 'freq': '100100000', 'bw': '200000',
                'gain': '50', 'sample_rate': '2100000', 'agc_mode': 'manual'}

    def tx_stats(self):
        return None


def test_fm_tx_enter_while_loading_does_not_stack_decodes(monkeypatch, tmp_path):
    """Repeat Enter while a decode is in flight must not start another.

    Overlapping decodes clobbered the shared load result and could surface
    as a spurious 'load failed'.
    """
    import threading
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView

    started: list[str] = []
    gate = threading.Event()

    def slow_load(path, rate):
        started.append(path)
        gate.wait(2.0)
        return [0.5] * 4410

    monkeypatch.setattr(fmmod, 'load_audio', slow_load)
    view = FmTxView()
    view.daemon = _StreamDaemon()
    song = tmp_path / 'song.mp3'
    song.write_bytes(b'x')
    view.command([str(song)])

    view._enter()
    view.tick(0.016)
    view._enter()   # still loading: ignored
    view.tick(0.016)
    view._enter()   # still loading: ignored
    assert len(started) == 1

    gate.set()
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert 'load failed' not in view._status_msg
    assert view._state in ('loaded', 'streaming', 'draining', 'done')


def test_fm_tx_load_launch_race_does_not_report_failure(monkeypatch, tmp_path):
    """A tick between publishing and starting the decode thread is not done.

    Keys are handled on the reader thread while tick runs on the main thread.
    A not-yet-started thread reports is_alive() == False, which used to look
    like a finished decode and surface as a spurious 'load failed'.
    """
    import threading
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView

    real_thread = threading.Thread
    launched = threading.Event()
    release = threading.Event()

    class SlowStartThread:
        def __init__(self, target, args=(), daemon=None):
            self._target = target
            self._args = args
            self._daemon = daemon
            self._thread = None

        def start(self):
            launched.set()
            release.wait(2.0)
            self._thread = real_thread(
                target=self._target, args=self._args, daemon=self._daemon)
            self._thread.start()

        def is_alive(self):
            return bool(self._thread and self._thread.is_alive())

        def join(self, timeout=None):
            if self._thread is not None:
                self._thread.join(timeout)

    shim = type('ThreadingShim', (), {'Thread': SlowStartThread})
    monkeypatch.setattr(fmmod, 'threading', shim)
    monkeypatch.setattr(fmmod, 'load_audio',
                        lambda path, rate: [0.5] * 4410)

    view = FmTxView()
    view.daemon = _StreamDaemon()
    paths = []
    for name in ('a.mp3', 'b.mp3'):
        path = tmp_path / name
        path.write_bytes(b'x')
        paths.append(path)
    view._queue([str(path) for path in paths])
    view._selected = 0

    key = real_thread(target=lambda: view._load_track(0))
    key.start()
    assert launched.wait(2.0)

    view.tick(0.016)
    assert view._playlist[0].error == ''
    assert view._status_msg != 'load failed'

    release.set()
    key.join(timeout=2.0)
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert view._playlist[0].error == ''
    assert view._state in ('loaded', 'streaming', 'draining')


def test_fm_tx_load_failure_reports_error(monkeypatch, tmp_path):
    """A decode failure surfaces the decoder's reason, not a blank state."""
    import shesha.tui.fm as fmmod
    from shesha.fm_audio import AudioLoadError
    from shesha.tui.fm import FmTxView

    def failing_load(path, rate):
        raise AudioLoadError('Invalid data found when processing input')

    monkeypatch.setattr(fmmod, 'load_audio', failing_load)
    view = FmTxView()
    view.daemon = _StreamDaemon()
    broken = tmp_path / 'broken.mp3'
    broken.write_bytes(b'x')
    view.command([str(broken)])
    assert len(view._playlist) == 1
    view._enter()
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert view._state == 'idle'
    assert 'Invalid data' in view._status_msg
    assert 'Invalid data' in view._playlist[0].error


def test_fm_tx_view_on_exit_resets():
    """Leaving the TX view resets to idle."""
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.on_exit()
    assert view._state == 'idle'


def test_fm_tx_view_completes_path(tmp_path):
    from shesha.tui.fm import FmTxView
    (tmp_path / "song.mp3").touch()
    view = FmTxView()
    candidates = view.command_completions(f'{tmp_path}/son')
    assert any("song.mp3" in c for c in candidates)


def test_fm_tx_view_completion_hides_non_audio(tmp_path):
    import os
    from shesha.tui.fm import FmTxView
    (tmp_path / 'song.mp3').touch()
    (tmp_path / 'notes.txt').touch()
    (tmp_path / 'covers').mkdir()
    view = FmTxView()
    candidates = view.command_completions(f'{tmp_path}/')
    names = {os.path.basename(c.rstrip(os.sep)) for c in candidates}
    assert 'song.mp3' in names
    assert 'notes.txt' not in names
    assert 'covers' in names


def test_fm_tx_view_has_no_word_completions():
    from shesha.tui.fm import FmTxView
    assert FmTxView().word_completions() == []


def test_fm_app_has_tx_view():
    """FmApp registers the TX view."""
    app = FmApp()
    assert 'tx' in app.views


# ─── FmTxView integration ────────────────────────────────────────────


def test_fm_tx_full_lifecycle(tmp_path):
    """Load file → stream → drain → done, end-to-end with DummyDaemon."""
    import struct
    from shesha.tui.fm import FmTxView

    # Create a short .f32 test file (0.1s at 44.1 kHz)
    samples = [0.5] * 4410
    path = tmp_path / "tone.f32"
    path.write_bytes(struct.pack(f'<{len(samples)}f', *samples))

    config = Config(str(tmp_path / 'c.json'))
    daemon = DummyDaemon(config)
    daemon.set_mode(3)

    app = FmApp()
    app.switch_view('tx')
    view = app.active_view
    assert isinstance(view, FmTxView)
    view.daemon = daemon

    # Queue the file, then play the selection.
    view.command([str(path)])
    assert view._state == 'idle'
    assert len(view._playlist) == 1
    view._enter()
    assert view._state == 'loading'

    # Enter plays straight through: load completion auto-starts the stream.
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    assert view._state == 'streaming', (
        f'stuck in {view._state}: {view._status_msg}')
    assert daemon.get_state()['tx_busy'] == '1'

    # Tick until all audio is pushed (should be fast with DummyDaemon)
    for _ in range(500):
        view.tick(0.016)
        if view._state in ('draining', 'done'):
            break
    assert view._state in ('draining', 'done'), f'stuck in {view._state}'

    # If draining, tick a few more times (DummyDaemon returns tx_active=False
    # after fm_tx_stream_end clears tx_busy)
    if view._state == 'draining':
        for _ in range(20):
            view.tick(0.016)
            if view._state == 'done':
                break
    assert view._state == 'done'

    # tx_stats should reflect idle
    stats = daemon.tx_stats()
    assert stats.tx_active is False


def test_fm_tx_pause_keeps_playhead(tmp_path):
    """Pausing mid-stream keeps the audio and the playhead; Space resumes."""
    import struct
    from shesha.tui.fm import FmTxView

    samples = [0.5] * (44100 * 3)  # 3 seconds
    path = tmp_path / "long.f32"
    path.write_bytes(struct.pack(f'<{len(samples)}f', *samples))

    config = Config(str(tmp_path / 'c.json'))
    daemon = DummyDaemon(config)
    daemon.set_mode(3)
    daemon._clock = lambda: 0.0   # no playout drift; this test drives _sent

    app = FmApp()
    app.switch_view('tx')
    view = app.active_view
    view.daemon = daemon

    view.command([str(path)])
    view._enter()
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    assert view._state == 'streaming'

    # Pause mid-stream: audio and playhead are kept.
    position = view._sent
    view._space()
    assert view._state == 'paused'
    assert view._audio
    assert view._resume_index == position

    # Space again resumes from the same spot, still short of the end.
    view._space()
    assert view._state == 'streaming'
    assert view._sent >= position
    view._stop_stream()


def test_fm_tx_pause_uses_emitted_playhead():
    """Pause resumes at the DAC's emitted position, not the pushed one.

    The client pushes up to the ~1 s TX lead ahead of the DAC, so saving
    ``_sent`` makes resume skip that lead.  ``tx_stats().emitted`` is the
    on-air sample count; the resume index must come from it.
    """
    from shesha.protocol import TxStats
    from shesha.tui.fm import FmTxView

    class _StatsDaemon:
        emitted = 0

        def tx_stats(self):
            return TxStats(
                tx_active=True, fm_active=True, end_of_stream=False,
                dac_rate=2_100_000, fed=0, emitted=self.emitted,
                elapsed_us=0, depth=0, max_depth=0, high_water=0,
                underruns=0, ring_fill=0, ring_size=262144, audio_rate=44100,
                audio_consumed=0, starved_samples=0, push_rejects=0,
                iq_active=False, iq_ring_fill=0, iq_push_rejects=0,
                iq_starved=0)

        def tx_stop(self):
            self.stopped = True

    view = FmTxView()
    view.daemon = _StatsDaemon()
    view._audio = [0.1] * 44100
    view._sent = 44100                      # pushed a full second ahead
    view._state = 'streaming'
    # The DAC has emitted a quarter second of audio's worth of samples.
    view.daemon.emitted = int(2_100_000 * 0.25)

    view._space()

    assert view._state == 'paused'
    assert view._resume_index == int(44100 * 0.25)
    assert view._sent == view._resume_index


def test_fm_tx_stop_sends_tx_stop():
    """User stop must issue CMD_TX_STOP, not a drain-only stream end."""
    from shesha.tui.fm import FmTxView

    class _Spy:
        def __init__(self):
            self.stopped = False
            self.ended = False

        def tx_stop(self):
            self.stopped = True

        def fm_tx_stream_end(self):
            self.ended = True

    view = FmTxView()
    spy = _Spy()
    view.daemon = spy
    view._audio = [0.1, 0.2]
    view._state = 'streaming'
    view._stop_stream()
    assert spy.stopped
    assert not spy.ended
    assert view._state == 'loaded'


def test_fm_tx_on_exit_stops_active_stream():
    """Leaving the view must halt the transmitter, not orphan it."""
    from shesha.tui.fm import FmTxView

    class _Spy:
        def __init__(self):
            self.stopped = False

        def tx_stop(self):
            self.stopped = True

    view = FmTxView()
    spy = _Spy()
    view.daemon = spy
    view._state = 'streaming'
    view.on_exit()
    assert spy.stopped
    assert view._state == 'idle'


def test_fm_tx_enter_stops_space_pauses(tmp_path):
    """Enter plays/stops/resets; Space pauses and resumes from the spot."""
    import struct
    from shesha.tui.fm import FmTxView

    samples = [0.5] * (44100 * 3)
    path = tmp_path / "tone.f32"
    path.write_bytes(struct.pack(f'<{len(samples)}f', *samples))

    config = Config(str(tmp_path / 'c.json'))
    daemon = DummyDaemon(config)
    daemon.set_mode(3)
    now = [1000.0]
    daemon._clock = lambda: now[0]

    app = FmApp()
    app.switch_view('tx')
    view = app.active_view
    view.daemon = daemon
    view._clock = lambda: now[0]

    view.command([str(path)])
    view._enter()
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    assert view._state == 'streaming'
    assert daemon.get_state()['tx_busy'] == '1'

    # Let the DAC emit some audio so the playhead is past zero.
    now[0] += 0.1

    # Space pauses (carrier down, playhead kept), Space resumes.
    view._space()
    assert view._state == 'paused'
    assert daemon.get_state()['tx_busy'] == '0'
    spot = view._resume_index
    assert spot > 0
    view._space()
    assert view._state == 'streaming'
    assert daemon.get_state()['tx_busy'] == '1'

    # Enter while playing stops and resets to the top.
    view._enter()
    assert view._state == 'loaded'
    assert view._sent == 0
    assert view._resume_index == 0
    assert daemon.get_state()['tx_busy'] == '0'

    # Enter again plays from the top without re-decoding.
    view._enter()
    assert view._state == 'streaming'
    assert daemon.get_state()['tx_busy'] == '1'
    view._stop_stream()


def test_fm_tx_enter_on_other_track_switches_in_one_press(monkeypatch, tmp_path):
    """Enter on a different track stops the current one and plays the new one.

    The old behaviour stopped and parked, so switching while playing took
    Enter twice: once to stop, once to start the selection.
    """
    import struct
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView

    monkeypatch.setattr(fmmod, 'load_audio', lambda path, rate: [0.5] * 4410)

    class _SpyDaemon(_StreamDaemon):
        def __init__(self):
            self.stops = 0

        def tx_stop(self):
            self.stops += 1

    paths = []
    for name in ('one.f32', 'two.f32'):
        p = tmp_path / name
        p.write_bytes(struct.pack(f'<{4410}f', *([0.5] * 4410)))
        paths.append(p)

    view = FmTxView()
    spy = _SpyDaemon()
    view.daemon = spy
    view._queue([str(p) for p in paths])
    view._select_to(0)
    view._enter()
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert view._state == 'streaming'
    assert view._playing == 0

    view._select_to(1)
    view._enter()

    assert spy.stops == 1
    assert view._playing == 1
    assert view._state == 'loading'
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert view._state in ('streaming', 'draining', 'done')
    assert view._playing == 1


def test_fm_tx_skip_plays_adjacent_track(tmp_path):
    """Next/previous selects the neighbour and plays it from the top."""
    import struct
    from shesha.tui.fm import FmTxView

    paths = []
    for name in ('one.f32', 'two.f32'):
        p = tmp_path / name
        p.write_bytes(struct.pack(f'<{4410}f', *([0.5] * 4410)))
        paths.append(p)

    config = Config(str(tmp_path / 'c.json'))
    daemon = DummyDaemon(config)
    daemon.set_mode(3)

    app = FmApp()
    app.switch_view('tx')
    view = app.active_view
    view.daemon = daemon

    view.command([str(paths[0])])
    view.command([str(paths[1])])
    view._select_to(0)
    view._enter()
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    assert view._playing == 0

    view._skip(1)
    assert view._selected == 1
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    assert view._playing == 1
    assert view._state in ('streaming', 'draining', 'done')
    view._stop_stream()


# ─── Task 5: trace_color ─────────────────────────────────────────────

from shesha.tui.fm import trace_color
from shesha.tui.theme import DEFAULT_PALETTE


def test_trace_color_normal_is_accent():
    assert trace_color(0.5, DEFAULT_PALETTE) == DEFAULT_PALETTE['accent']


def test_trace_color_silence_is_still_visible_accent():
    """A silent carrier is not an absent station; there is no quiet state."""
    assert trace_color(0.0, DEFAULT_PALETTE) == DEFAULT_PALETTE['accent']


def test_trace_color_clipping_is_warn():
    assert trace_color(0.99, DEFAULT_PALETTE) == DEFAULT_PALETTE['warn']


def test_trace_color_boundaries():
    for peak in (0.0, 0.05, 0.1, 0.9, 0.95, 1.0, 1.5):
        assert trace_color(peak, DEFAULT_PALETTE) in (
            DEFAULT_PALETTE['accent'], DEFAULT_PALETTE['warn'])


# ─── FmTxView: continuous play ────────────────────────────────────────


def test_fm_app_registers_continuous_config_key():
    from shesha.tui.fm import FmApp
    keys = FmApp.config_keys()
    assert 'fm.tx.continuous' in keys
    assert keys['fm.tx.continuous'].type == 'bool'


def test_fm_tx_keybinds_include_seek_and_continuous():
    from shesha.tui.fm import FmTxView
    keys = FmTxView().keybinds()
    for k in ('<', '>', 'c', ',', '.'):
        assert k in keys


def test_fm_tx_continuous_toggle_persists_across_views(tmp_path):
    from shesha.tui.fm import FmApp, FmTxView
    config = Config(str(tmp_path / 'c.json'))
    config.register_keys(FmApp.config_keys())
    view = FmTxView()
    view.config = config
    view.on_enter()
    assert view._continuous is False

    view._toggle_continuous()
    assert view._continuous is True
    assert config.get_bool('fm.tx.continuous') is True

    fresh = FmTxView()
    fresh.config = config
    fresh.on_enter()
    assert fresh._continuous is True


def test_fm_tx_continuous_key_toggles():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.keybinds()['c'](1)
    assert view._continuous is True
    view.keybinds()['c'](1)
    assert view._continuous is False


def test_fm_tx_finish_playback_advances_when_continuous(monkeypatch):
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView, Track
    monkeypatch.setattr(fmmod, 'load_audio', lambda p, r: [0.5] * 100)
    view = FmTxView()
    view.daemon = _StreamDaemon()
    view._continuous = True
    view._playlist = [Track('a.mp3'), Track('b.mp3')]
    view._playing = 0
    view._state = 'draining'
    view._finish_playback()
    assert view._selected == 1
    assert view._playing == 1
    assert view._state == 'loading'
    view._load_thread.join(timeout=2.0)


def test_fm_tx_finish_playback_stops_when_not_continuous():
    from shesha.tui.fm import FmTxView, Track
    view = FmTxView()
    view._continuous = False
    view._playlist = [Track('a.mp3'), Track('b.mp3')]
    view._playing = 0
    view._finish_playback()
    assert view._state == 'done'


def test_fm_tx_finish_playback_wraps_at_last_track(monkeypatch):
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView, Track
    monkeypatch.setattr(fmmod, 'load_audio', lambda p, r: [0.5] * 100)
    view = FmTxView()
    view.daemon = _StreamDaemon()
    view._continuous = True
    view._playlist = [Track('a.mp3'), Track('b.mp3')]
    view._playing = 1
    view._state = 'draining'
    view._finish_playback()
    assert view._selected == 0
    assert view._playing == 0
    assert view._state == 'loading'
    view._load_thread.join(timeout=2.0)


def test_fm_tx_finish_playback_repeats_single_track(monkeypatch):
    import shesha.tui.fm as fmmod
    from shesha.tui.fm import FmTxView, Track
    monkeypatch.setattr(fmmod, 'load_audio', lambda p, r: [0.5] * 100)
    view = FmTxView()
    view.daemon = _StreamDaemon()
    view._continuous = True
    view._playlist = [Track('a.mp3')]
    view._playing = 0
    view._state = 'draining'
    view._finish_playback()
    assert view._selected == 0
    assert view._playing == 0
    assert view._state == 'loading'
    view._load_thread.join(timeout=2.0)


# ─── FmTxView: playing panel ─────────────────────────────────────────


def _tx_player_rows(view, width=80):
    from shesha.tui.pytui import Text
    return [Text(row).strip_ansi() for row in view._player_lines(width)]


def test_fm_tx_panel_is_titled_playing():
    from shesha.tui.fm import FmTxView
    rows = _tx_player_rows(FmTxView())
    assert 'now playing' not in '\n'.join(rows)
    assert '\u2500 playing ' in rows[0]


def test_fm_tx_title_prefers_artist_and_title_then_filename():
    from shesha.mp3 import TrackTags
    from shesha.tui.fm import FmTxView, Track
    view = FmTxView()
    view._playlist = [Track(
        'song.mp3', tags=TrackTags(title='Sunlight', artist='The Band'))]
    view._playing = 0
    assert 'The Band - Sunlight' in _tx_player_rows(view)[1]
    assert '\u266a' not in _tx_player_rows(view)[1]

    view._playlist = [Track('/music/plain.mp3')]
    assert ' plain.mp3' in _tx_player_rows(view)[1]


def test_fm_tx_pause_and_seek_clear_status_message():
    from shesha.tui.fm import FmTxView, Track
    view = FmTxView()
    view.daemon = _StreamDaemon()
    view._playlist = [Track('a.mp3', duration=240.0)]
    view._playing = 0
    view._audio = [0.0] * 44100
    view._audio_rate = 44100
    view._state = 'streaming'
    view._sent = 4410
    view._status_msg = 'old status'

    view._space()
    assert view._state == 'paused'
    assert view._status_msg == ''

    view._status_msg = 'old status'
    view._seek(1)
    assert view._state == 'paused'
    assert view._status_msg == ''


def test_fm_tx_seek_bar_drops_label_and_fills_the_row():
    from shesha.tui.fm import FmTxView, Track
    view = FmTxView()
    view._playlist = [Track('a.mp3', duration=240.0)]
    view._playing = 0
    view._audio = [0.0] * 44100
    view._audio_rate = 44100
    view._state = 'paused'
    view._sent = 4410

    row = next(r for r in _tx_player_rows(view) if ' / ' in r)
    assert 'seek' not in row
    assert len(row) == 80
    bar = row[row.index('[') + 1:row.index(']')]
    assert len(bar) >= 60


def test_fm_tx_transport_buttons_use_foreground_when_off():
    from shesha.tui.fm import FmTxView
    from shesha.tui.pytui import Text
    view = FmTxView()
    rows = view._player_lines(80)
    transport = next(r for r in rows if '[\u25c0\u25c0]' in Text(r).strip_ansi())
    off = Text('[\u25c0\u25c0]').style({'fg': view.style['fg']})
    assert off in transport


# ─── FmTxView: seeking ────────────────────────────────────────────────


def _tx_view_with_track(tmp_path, seconds=10.0):
    """A TX view streaming a decoded *seconds*-long tone."""
    import struct
    from shesha.tui.fm import FmTxView

    samples = [0.5] * int(44100 * seconds)
    path = tmp_path / 'long.f32'
    path.write_bytes(struct.pack(f'<{len(samples)}f', *samples))
    config = Config(str(tmp_path / 'c.json'))
    daemon = DummyDaemon(config)
    daemon.set_mode(3)
    daemon._clock = lambda: 0.0   # no playout drift; these tests drive _sent
    app = FmApp()
    app.switch_view('tx')
    view = app.active_view
    assert isinstance(view, FmTxView)
    view.daemon = daemon
    view.command([str(path)])
    view._enter()
    view._load_thread.join(timeout=5.0)
    view.tick(0.016)
    return view


def test_fm_tx_seek_forward_and_back(tmp_path):
    view = _tx_view_with_track(tmp_path)
    assert view._state == 'streaming'
    start = view._sent
    step = int(5.0 * view._audio_rate)

    view.keybinds()['>'](1)                    # +5 s
    assert view._sent - start >= step
    assert view._state == 'streaming'
    fwd = view._sent

    view.keybinds()['<'](1)                    # -5 s
    assert view._sent < fwd
    view._stop_stream()


def test_fm_tx_seek_count_prefix_scales(tmp_path):
    view = _tx_view_with_track(tmp_path, seconds=60.0)
    start = view._sent
    step = int(5.0 * view._audio_rate)
    view.keybinds()['>'](3)                    # +15 s
    assert view._sent - start >= 3 * step
    view._stop_stream()


def test_fm_tx_seek_clamps_at_edges(tmp_path):
    view = _tx_view_with_track(tmp_path)
    view.keybinds()['<'](100)                  # far before the top
    assert view._sent <= int(view._audio_rate)
    view.keybinds()['>'](100)                  # far past the end
    assert view._state == 'done'
    assert view._sent <= len(view._audio)


def test_fm_tx_seek_without_audio_reports():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._seek(1)
    assert 'load a track first' in view._status_msg


def test_fm_tx_seek_streaming_keeps_carrier_and_flushes(tmp_path):
    """Seeking while streaming must not tear the carrier down.

    The old path halted the stream and restarted it, costing the ~1 s
    prefill and a dropout.  A seek now flushes the daemon's queued audio
    and jumps the feed, leaving the stream running.
    """
    view = _tx_view_with_track(tmp_path)
    assert view._state == 'streaming'
    start = view._sent
    step = int(5.0 * view._audio_rate)
    calls = {'stop': 0, 'flush': 0}
    orig_stop = view.daemon.tx_stop
    view.daemon.tx_stop = lambda: (
        calls.__setitem__('stop', calls['stop'] + 1), orig_stop())
    view.daemon.fm_tx_stream_flush = lambda: (
        calls.__setitem__('flush', calls['flush'] + 1))

    view.keybinds()['>'](1)

    assert calls['stop'] == 0
    assert calls['flush'] == 1
    assert view._state == 'streaming'
    assert view._sent - start >= step
    assert view._playhead() == view._sent
    view._stop_stream()


def _stats_daemon(**overrides):
    from shesha.protocol import TxStats

    class _D:
        def __init__(self):
            self.emitted = 0
            self.depth = 0
            self.ring_fill = 0
            self.underruns = 0
            self.fed = 0
            self.flushed = 0
            for name, value in overrides.items():
                setattr(self, name, value)

        def tx_stats(self):
            return TxStats(
                tx_active=True, fm_active=True, end_of_stream=False,
                dac_rate=2_100_000, fed=self.fed, emitted=self.emitted,
                elapsed_us=0, depth=self.depth, max_depth=0, high_water=0,
                underruns=self.underruns, ring_fill=self.ring_fill,
                ring_size=262144, audio_rate=44100,
                audio_consumed=0, starved_samples=0, push_rejects=0,
                iq_active=False, iq_ring_fill=0, iq_push_rejects=0,
                iq_starved=0)

        def fm_tx_stream_flush(self):
            self.flushed += 1
            return True

        def tx_stop(self):
            pass

    return _D()


def test_fm_tx_polls_telemetry_at_a_sane_rate():
    """Rendering must not issue two tx_stats commands every frame."""
    view = FmTxView()
    daemon = _stats_daemon(emitted=2_100_000)
    calls = 0
    original = daemon.tx_stats

    def counting():
        nonlocal calls
        calls += 1
        return original()

    daemon.tx_stats = counting
    view.daemon = daemon
    view._state = 'streaming'
    view._audio = [0.0] * 100_000
    view._emit_base = 0
    now = [0.0]
    view._clock = lambda: now[0]

    for _ in range(60):
        now[0] += 1.0 / 60.0
        view._status_line(80)
        view._spectrum_samples()

    assert calls <= 5


def test_fm_tx_status_fields_keep_columns_when_values_jump():
    """Fast-changing telemetry must not shift the fields around."""
    from shesha.tui.pytui import Text

    view = FmTxView()
    view.daemon = _stats_daemon()
    view._state = 'streaming'

    def line(**values):
        for name, value in values.items():
            setattr(view.daemon, name, value)
        return Text(view._status_line(80)).strip_ansi()

    small = line(depth=21_000, ring_fill=8_820, underruns=0,
                 fed=1_000_000, emitted=2_000_000)
    large = line(depth=2_100_000, ring_fill=176_400, underruns=9999,
                 fed=99_000_000, emitted=88_000_000)

    assert len(small) == 80
    assert len(large) == 80
    for label in ('depth', 'ring', 'rate', 'underruns', 'fed', 'emitted'):
        assert small.index(label) == large.index(label)


def test_fm_tx_seek_rebases_emitted_playhead():
    """After a keep-carrier seek the emitted playhead tracks the new target.

    ``emitted`` never resets while the stream runs, so the client must
    rebase it: the queued (old) audio still on the DAC is accounted for
    before the new target's samples are counted.
    """
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.daemon = _stats_daemon()
    view._audio = [0.1] * 1_000_000
    view._state = 'streaming'
    view._sent = 44_100
    view._index_base = 0
    view._emit_base = 0
    view.daemon.emitted = 2_100_000        # 1 s of DAC: audio at sample 44100
    view.daemon.depth = 21_000             # 0.01 s still queued

    view._seek(2)                          # +10 s

    target = 44_100 + 2 * 5 * 44_100
    assert view.daemon.flushed == 1
    assert view._sent == target
    assert view._playhead() == target       # not yet past the rebase point

    # Once the queued audio drains, emitted advances the playhead.
    view.daemon.emitted = view._emit_base + 2_100_000
    assert view._playhead() == target + 44_100


def test_fm_tx_seek_past_end_while_streaming_parks_done(tmp_path):
    view = _tx_view_with_track(tmp_path)
    view.keybinds()['>'](1000)             # far past the end
    assert view._state == 'done'
    assert view._sent <= len(view._audio)


def test_fm_tx_transport_button_flashes_on_keypress():
    """A transport keypress lights its button for a moment, then clears."""
    from shesha.tui.animate import Animator
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view.animator = Animator()
    assert not view._lit('tx_seek_fwd')
    view._flash('tx_seek_fwd')
    assert view._lit('tx_seek_fwd')
    view.animator.tick(0.2)          # past the 0.15 s flash
    assert not view._lit('tx_seek_fwd')


# ─── Shared helpers ──────────────────────────────────────────────────


def test_pad_fills_short_list():
    """Missing lines are filled with spaces to the target height."""
    result = _pad(['abc'], 3, 3)
    assert len(result) == 3
    assert result[0] == 'abc'
    assert result[1] == '   '
    assert result[2] == '   '


def test_pad_truncates_long_list():
    """Excess lines are dropped when the list exceeds the target height."""
    result = _pad(['a', 'b', 'c', 'd'], 1, 2)
    assert result == ['a', 'b']


def test_render_dial_styled_exists_and_callable():
    """The module-level dial renderer is importable and callable."""
    assert callable(_render_dial_styled)


# ─── FmTxView: RDS transmit config ───────────────────────────────────


class _RdsDaemon:
    def __init__(self) -> None:
        self.rds_calls: list = []
        self.start_rds: list = []

    def set_rds(self, rds) -> bool:
        self.rds_calls.append(rds)
        return True

    def fm_tx_stream_start(self, audio, *, audio_rate=44100, rds=None, **kw):
        self.start_rds.append(rds)
        return True

    def get_state(self):
        return {}


def _tx_view_with_rds(tmp_path):
    from shesha.tui.fm import FmApp, FmTxView
    config = Config(str(tmp_path / 'c.json'))
    config.register_keys(FmApp.config_keys())
    app = FmApp()
    app.views['tx'].config = config
    app.views['tx'].on_enter()
    return app, app.views['tx'], config


def test_fm_app_registers_rds_key_and_command():
    keys = FmApp.config_keys()
    assert keys['fm.tx.rds.on'].type == 'bool'
    assert keys['fm.tx.rds.ps'].type == 'str'
    assert keys['fm.tx.rds.rt'].type == 'str'
    assert keys['fm.tx.rds.pi'].min == 0 and keys['fm.tx.rds.pi'].max == 0xFFFF
    assert keys['fm.tx.rds.pty'].min == 0 and keys['fm.tx.rds.pty'].max == 31
    assert 'rds' in FmApp().commands


def test_fm_rds_command_sets_and_persists(tmp_path):
    app, view, config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()

    assert app.execute_command('rds', ['pi', '8203'], daemon)[0] is None
    err, _ = app.execute_command('rds', ['on'], daemon)
    assert err is None and view._rds_on
    assert config.get_bool('fm.tx.rds.on') is True

    assert app.execute_command('rds', ['ps', 'TESTFM'], daemon)[0] is None
    assert view._rds_ps == 'TESTFM  '
    assert config.get('fm.tx.rds.ps') == 'TESTFM  '

    assert app.execute_command('rds', ['rt', 'hello'], daemon)[0] is None
    assert view._rds_rt == 'hello'.ljust(64)

    assert app.execute_command('rds', ['pi', '8203'], daemon)[0] is None
    assert view._rds_pi == 0x8203
    assert config.get('fm.tx.rds.pi') == '33283'

    assert app.execute_command('rds', ['pty', '10'], daemon)[0] is None
    assert view._rds_pty == 10

    assert app.execute_command('rds', ['off'], daemon)[0] is None
    assert not view._rds_on


def test_fm_rds_config_keys_validate(tmp_path):
    config = Config(str(tmp_path / 'c.json'))
    config.register_keys(FmApp.config_keys())
    config.set('fm.tx.rds.pty', '31')
    config.set('fm.tx.rds.pi', '65535')
    with pytest.raises(ValueError):
        config.set('fm.tx.rds.pty', '32')
    with pytest.raises(ValueError):
        config.set('fm.tx.rds.pi', '65536')
    with pytest.raises(ValueError):
        config.set('fm.tx.rds.on', 'maybe')


def test_fm_rds_command_validates(tmp_path):
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()

    assert app.execute_command('rds', ['ps', 'TOOLONG99'], daemon)[0] is not None
    assert app.execute_command('rds', ['pty', '32'], daemon)[0] is not None
    assert app.execute_command('rds', ['pi', 'nope'], daemon)[0] is not None
    assert app.execute_command('rds', ['pi', '10000'], daemon)[0] is not None
    assert app.execute_command('rds', ['bogus'], daemon)[0] is not None
    # Nothing was persisted or sent.
    assert daemon.rds_calls == []


def test_fm_rds_command_updates_live_stream(tmp_path):
    from shesha.protocol import RdsTxConfig
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view._state = 'streaming'
    view._rds_on = True
    view._rds_pi = 0x1111

    err, _ = app.execute_command('rds', ['ps', 'LIVE'], daemon)
    assert err is None
    assert len(daemon.rds_calls) == 1
    sent = daemon.rds_calls[0]
    assert isinstance(sent, RdsTxConfig)
    assert sent.enabled and sent.ps == 'LIVE    ' and sent.pi == 0x1111


def test_fm_rds_not_sent_when_idle(tmp_path):
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view._state = 'idle'
    app.execute_command('rds', ['on'], daemon)
    assert daemon.rds_calls == []


def test_fm_rds_not_sent_when_paused(tmp_path):
    """Pausing drops the stream; the setting still applies on resume."""
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view._state = 'paused'
    view._rds_pi = 0x8203
    err, _ = app.execute_command('rds', ['on'], daemon)
    assert err is None
    assert view._rds_on
    assert daemon.rds_calls == []


def test_fm_tx_stream_start_passes_rds_config(tmp_path):
    from shesha.protocol import RdsTxConfig
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view.daemon = daemon
    view._audio = [0.5] * 44100
    view._audio_rate = 44100
    view._state = 'loaded'
    view._rds_on = True
    view._rds_ps = 'TESTFM  '
    view._rds_pi = 0x8203
    view._rds_pty = 10

    view._start_streaming()
    assert len(daemon.start_rds) == 1
    cfg = daemon.start_rds[0]
    assert isinstance(cfg, RdsTxConfig)
    assert cfg.enabled and cfg.ps == 'TESTFM  ' and cfg.pi == 0x8203
    assert cfg.pty == 10


def test_fm_tx_rds_off_passes_disabled_config(tmp_path):
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view.daemon = daemon
    view._audio = [0.5] * 44100
    view._audio_rate = 44100
    view._state = 'loaded'
    view._rds_on = False

    view._start_streaming()
    assert daemon.start_rds[0] is not None
    assert daemon.start_rds[0].enabled is False


def test_fm_tx_rds_does_not_scale_audio(monkeypatch, tmp_path):
    """The client always loads at full scale; the daemon owns RDS headroom.

    Pre-scaling here made a live RDS toggle desync the level from the
    subcarrier state until the track was reloaded.  The modulator now
    applies the headroom whenever the subcarriers are on.
    """
    import shesha.tui.fm as fmmod

    monkeypatch.setattr(fmmod, 'load_audio', lambda p, r: [0.5] * 44100)
    app, view, _config = _tx_view_with_rds(tmp_path)
    view.daemon = _RdsDaemon()
    view._rds_on = True
    track = tmp_path / 'tone.mp3'
    track.write_bytes(b'x')
    view.command([str(track)])
    view._enter()
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert view._audio
    assert max(abs(s) for s in view._audio) == pytest.approx(1.0)


def test_fm_tx_rds_disabled_keeps_full_audio_peak(monkeypatch, tmp_path):
    import shesha.tui.fm as fmmod

    monkeypatch.setattr(fmmod, 'load_audio', lambda p, r: [0.5] * 44100)
    app, view, _config = _tx_view_with_rds(tmp_path)
    view.daemon = _RdsDaemon()
    view._rds_on = False
    track = tmp_path / 'tone.mp3'
    track.write_bytes(b'x')
    view.command([str(track)])
    view._enter()
    view._load_thread.join(timeout=2.0)
    view.tick(0.016)
    assert max(abs(s) for s in view._audio) == pytest.approx(1.0)


# ─── FmTxView: ID3 tags -> RDS ───────────────────────────────────────


def _write_id3v2(path, **fields):
    """Write an ID3v2 tag-only file (no audio frames needed)."""
    from tests.mp3_fixtures import write_tagged

    write_tagged(path, fields)


def _tx_view_with_tagged_track(tmp_path, **fields):
    from shesha.tui.fm import Track

    app, view, config = _tx_view_with_rds(tmp_path)
    mp3 = tmp_path / 'song.mp3'
    _write_id3v2(mp3, **fields)
    view._playlist = [Track(path=str(mp3))]
    view._playing = 0
    return app, view, config


def test_fm_app_registers_rds_id3_key():
    keys = FmApp.config_keys()
    assert keys['fm.tx.rds.from_id3'].type == 'bool'
    assert keys['fm.tx.rds.from_id3'].default is False


def test_fm_rds_id3_command_toggles_and_persists(tmp_path):
    app, view, config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()

    err, _ = app.execute_command('rds', ['id3', 'on'], daemon)
    assert err is None and view._rds_from_id3
    assert config.get_bool('fm.tx.rds.from_id3') is True

    assert app.execute_command('rds', ['id3', 'off'], daemon)[0] is None
    assert not view._rds_from_id3
    assert config.get_bool('fm.tx.rds.from_id3') is False


def test_fm_tx_rds_config_uses_id3_when_enabled(tmp_path):
    _app, view, _config = _tx_view_with_tagged_track(
        tmp_path, title='Sunlight', artist='The Band', genre='Rock')
    view._rds_on = True
    view._rds_from_id3 = True
    view._rds_ps = 'TESTFM  '
    view._rds_pi = 0x8203
    view._rds_rt = 'manual'.ljust(64)
    view._rds_pty = 10

    cfg = view._rds_config()
    assert cfg.ps == 'TESTFM  '          # PS stays the station name
    assert cfg.pi == 0x8203
    assert cfg.rt == 'The Band - Sunlight'
    assert cfg.pty == 5                  # Rock


def test_fm_tx_rds_config_manual_when_id3_disabled(tmp_path):
    _app, view, _config = _tx_view_with_tagged_track(
        tmp_path, title='Sunlight', artist='The Band', genre='Rock')
    view._rds_from_id3 = False
    view._rds_rt = 'manual'.ljust(64)
    view._rds_pty = 10

    cfg = view._rds_config()
    assert cfg.rt == 'manual'.ljust(64)
    assert cfg.pty == 10


def test_fm_tx_rds_config_id3_falls_back_without_tags(tmp_path):
    from shesha.tui.fm import Track

    _app, view, _config = _tx_view_with_rds(tmp_path)
    untagged = tmp_path / 'plain.f32'
    untagged.write_bytes(b'\x00\x00\x00\x00')
    view._playlist = [Track(path=str(untagged))]
    view._playing = 0
    view._rds_from_id3 = True
    view._rds_rt = 'fallback'.ljust(64)
    view._rds_pty = 7

    cfg = view._rds_config()
    assert cfg.rt == 'fallback'.ljust(64)
    assert cfg.pty == 7


def test_fm_rds_id3_updates_live_stream(tmp_path):
    """Toggling ID3 on while streaming re-sends the derived fields."""
    _app, view, _config = _tx_view_with_tagged_track(
        tmp_path, title='Sunlight', artist='The Band', genre='Jazz')
    daemon = _RdsDaemon()
    view._state = 'streaming'
    view._rds_on = True

    err, _ = view.rds_command(['id3', 'on'], daemon)
    assert err is None
    assert daemon.rds_calls[-1].rt == 'The Band - Sunlight'
    assert daemon.rds_calls[-1].pty == 14


# ─── FmTxView: spectrum visualiser ───────────────────────────────────

import math as _math


def _tone(freq, rate, n, amp=1.0):
    return [amp * _math.sin(2.0 * _math.pi * freq * i / rate)
            for i in range(n)]


def test_spectrum_levels_pure_tone_peaks_in_matching_band():
    from shesha.tui.fm import _spectrum_levels
    levels = _spectrum_levels(_tone(1000.0, 44100, 1024), 44100, 12)
    assert len(levels) == 12
    assert levels.index(max(levels)) == 6
    assert max(levels) > 0.8
    assert levels[0] < 0.2


def test_spectrum_levels_silence_is_zero():
    from shesha.tui.fm import _spectrum_levels
    assert _spectrum_levels([0.0] * 1024, 44100, 12) == [0.0] * 12


def test_spectrum_levels_clamped_to_unit_range():
    from shesha.tui.fm import _spectrum_levels
    levels = _spectrum_levels(_tone(1000.0, 44100, 1024, amp=8.0), 44100, 12)
    assert all(0.0 <= v <= 1.0 for v in levels)


def test_spectrum_levels_deterministic():
    from shesha.tui.fm import _spectrum_levels
    s = _tone(440.0, 44100, 1024)
    assert (_spectrum_levels(s, 44100, 12)
            == _spectrum_levels(s, 44100, 12))


def _emitted_daemon(emitted: int, dac_rate: int = 44100):
    from types import SimpleNamespace

    class _Daemon:
        def tx_stats(self):
            return SimpleNamespace(emitted=emitted, dac_rate=dac_rate,
                                   tx_active=True, fed=emitted)

        def fm_tx_stream_start(self, audio, **kwargs):
            return True

    return _Daemon()


def test_fm_tx_spectrum_rows_fill_the_panel():
    from shesha.tui.fm import FmTxView, _SPECTRUM_ROWS
    from shesha.tui.pytui import Text, display_width
    tone = _tone(1000.0, 44100, 2048)
    view = FmTxView(clock=lambda: 1000.0)
    view._state = 'streaming'
    view._audio = tone
    view._audio_rate = 44100
    view.daemon = _emitted_daemon(len(tone))
    rows = view._spectrum_rows(72)          # panel width - 6
    assert len(rows) == _SPECTRUM_ROWS
    plains = [Text(r).strip_ansi() for r in rows]
    assert all(display_width(p) == 72 for p in plains)
    assert any('\u2580' in p for p in plains)


def test_fm_tx_spectrum_padding_is_symmetric():
    from shesha.tui.fm import FmTxView
    tone = _tone(1000.0, 44100, 2048)
    view = FmTxView(clock=lambda: 1000.0)
    view._state = 'streaming'
    view._audio = tone
    view._audio_rate = 44100
    view.daemon = _emitted_daemon(len(tone))

    wave_rows = [r for r in _tx_player_rows(view, 80) if '\u2580' in r]
    assert wave_rows
    assert all(r.startswith('\u2502  \u2580') for r in wave_rows)
    assert all(r.endswith('  \u2502') for r in wave_rows)


def test_spectrum_reads_emitted_audio_not_the_push_buffer():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._audio = [float(i) for i in range(10_000)]
    view._audio_rate = 44100
    view._sent = 9_000                     # pushed far ahead
    view.daemon = _emitted_daemon(5_000)   # only 5000 are on air
    half = 1024 // 2
    assert (view._spectrum_samples()
            == view._audio[5_000 - half:5_000 + half])


def test_spectrum_advances_between_cached_telemetry_polls():
    """The FFT window moves every frame, not once per telemetry poll.

    tx_stats is a blocking round-trip cached at 4 Hz.  The visualiser must
    interpolate the emitted counter between polls, or the bars advance in
    250 ms steps and look frozen.
    """
    from array import array
    from types import SimpleNamespace
    from shesha.tui.fm import FmTxView

    now = [1000.0]
    polls = []

    class _Daemon:
        def tx_stats(self):
            polls.append(now[0])
            return SimpleNamespace(
                tx_active=True, dac_rate=2_100_000, fed=10_000_000,
                emitted=int((now[0] - 999.0) * 2_100_000))

    view = FmTxView(clock=lambda: now[0])
    view.daemon = _Daemon()
    view._audio = array('f', [0.0]) * 100_000
    view._audio_rate = 44100
    view._state = 'streaming'
    view._sent = len(view._audio)

    first = view._emitted_index()
    now[0] += 0.016
    second = view._emitted_index()

    assert len(polls) == 1          # still inside the 250 ms cache window
    assert second > first


def test_spectrum_polls_until_the_stream_emits():
    """Do not wait a quarter second to notice the first emitted sample.

    The daemon resets telemetry when a stream starts.  Until emitted is
    nonzero there is nothing to interpolate, so the visualiser polls each
    frame instead of showing a blank or stale window.
    """
    from array import array
    from types import SimpleNamespace
    from shesha.tui.fm import FmTxView

    now = [1000.0]
    polls = []

    class _Daemon:
        def tx_stats(self):
            polls.append(now[0])
            emitted = 0 if len(polls) < 3 else 44100
            return SimpleNamespace(
                tx_active=True, dac_rate=2_100_000, fed=10_000_000,
                emitted=emitted)

    view = FmTxView(clock=lambda: now[0])
    view.daemon = _Daemon()
    view._audio = array('f', [0.0]) * 100_000
    view._audio_rate = 44100
    view._state = 'streaming'
    view._sent = len(view._audio)
    view._start_sync_until = now[0] + 1.0

    assert view._emitted_index() is None
    now[0] += 0.016
    assert view._emitted_index() is None
    now[0] += 0.016
    assert view._emitted_index() is not None
    assert len(polls) == 3


def test_spectrum_window_clamps_at_track_end():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._audio = [float(i) for i in range(2_000)]
    view._audio_rate = 44100
    view._sent = len(view._audio)
    view.daemon = _emitted_daemon(len(view._audio))
    assert view._spectrum_samples() == view._audio[2_000 - 512:2_000]


def test_spectrum_waits_for_audio_to_actually_emit():
    from shesha.tui.fm import FmTxView
    from shesha.tui.pytui import Text
    view = FmTxView()
    view._state = 'streaming'
    view._audio = _tone(1000.0, 44100, 4096)
    view._audio_rate = 44100
    view.daemon = _emitted_daemon(0)
    assert view._spectrum_samples() is None
    rows = view._spectrum_rows(72)
    assert all(Text(r).strip_ansi().strip() == '' for r in rows)


def test_spectrum_state_resets_when_a_stream_starts():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._state = 'loaded'
    view._audio = [0.1] * 44100
    view._audio_rate = 44100
    view.daemon = _emitted_daemon(0)
    view._spec_levels = [1.0, 1.0]
    view._spec_peaks = [1.0, 1.0]
    view._start_streaming()
    assert view._spec_levels == []
    assert view._spec_peaks == []


def test_spectrum_bars_fall_at_winamp_rate():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._reset_spectrum()
    view._advance_spectrum([1.0, 1.0], 1 / 60)
    view._advance_spectrum([0.0, 0.0], 0.2)
    assert view._spec_levels == pytest.approx([0.4, 0.4], abs=0.01)
    view._advance_spectrum([0.0, 0.0], 0.2)
    assert view._spec_levels == [0.0, 0.0]


def test_spectrum_peak_caps_hang_then_drop():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    view._reset_spectrum()
    view._advance_spectrum([1.0], 1 / 60)
    view._advance_spectrum([0.0], 0.2)
    assert view._spec_peaks[0] > 0.9
    view._advance_spectrum([0.0], 0.4)
    assert 0.2 < view._spec_peaks[0] < 0.9
    view._advance_spectrum([0.0], 0.5)
    assert view._spec_peaks[0] == 0.0


def test_spectrum_quiet_tone_stays_near_the_floor():
    from shesha.tui.fm import _spectrum_levels
    levels = _spectrum_levels(_tone(1000.0, 44100, 2048, amp=0.03),
                              44100, 19)
    assert max(levels) < 0.2


def test_spectrum_gradient_is_anchored_to_the_canvas():
    from shesha.tui.fm import FmTxView
    view = FmTxView()
    assert view._bar_color(0, 10) == view.style['fg']
    assert view._bar_color(9, 10) == view.style['accent']


def test_fm_tx_spectrum_blank_when_idle():
    from shesha.tui.fm import FmTxView, _SPECTRUM_ROWS
    from shesha.tui.pytui import Text
    view = FmTxView()
    rows = view._spectrum_rows(72)
    assert len(rows) == _SPECTRUM_ROWS
    assert all(Text(r).strip_ansi().strip() == '' for r in rows)


# ─── FmTxView: queue-time metadata probe ─────────────────────────────


def test_fm_tx_queue_reads_tags_and_duration_up_front(tmp_path, monkeypatch):
    from tests.mp3_fixtures import write_mp3
    from shesha.tui.pytui import Text
    import shesha.tui.fm as fmmod

    mp3 = tmp_path / 'song.mp3'
    write_mp3(mp3, {'title': 'Sunlight', 'artist': 'The Band'}, frames=100)

    app, view, _config = _tx_view_with_rds(tmp_path)
    decoded: list = []
    monkeypatch.setattr(fmmod, 'load_audio',
                        lambda *a: decoded.append(a) or [])
    view.command([str(mp3)])

    track = view._playlist[0]
    assert track.tags.title == 'Sunlight'
    assert track.duration == pytest.approx(2.606, abs=1e-3)
    assert not track.loaded
    assert decoded == []          # queueing did not decode the file

    rows = [Text(r).strip_ansi() for r in view._playlist_lines(80, 6)]
    assert any('Sunlight' in row and '0:02' in row for row in rows)
    player = [Text(r).strip_ansi() for r in view._player_lines(80)]
    assert any('Sunlight' in row for row in player)


def test_fm_tx_rds_line_shows_derived_fields(tmp_path):
    _app, view, _config = _tx_view_with_tagged_track(
        tmp_path, title='Sunlight', artist='The Band', genre='Rock')
    view._rds_on = True
    view._rds_from_id3 = True
    view._rds_rt = 'manual'.ljust(64)
    view._rds_pty = 10

    line = view._rds_line()
    assert 'The Band - Sunlight' in line
    assert '5 Rock' in line
    assert 'manual' not in line


def test_fm_rds_command_strips_quotes(tmp_path):
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()

    assert app.execute_command('rds', ['ps', '"foobar"'], daemon)[0] is None
    assert view._rds_ps == 'foobar  '
    assert app.execute_command(
        'rds', ['rt', '"hello', 'world"'], daemon)[0] is None
    assert view._rds_rt == 'hello world'.ljust(64)


def test_fm_rds_requires_pi_before_enabling(tmp_path):
    app, view, _config = _tx_view_with_rds(tmp_path)
    daemon = _RdsDaemon()
    view._rds_pi = 0

    err, _ = app.execute_command('rds', ['on'], daemon)
    assert err is not None
    assert view._rds_on is False

    assert app.execute_command('rds', ['pi', '8203'], daemon)[0] is None
    assert app.execute_command('rds', ['on'], daemon)[0] is None
    assert view._rds_on is True
