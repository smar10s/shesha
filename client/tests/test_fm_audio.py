"""Tests for fm_audio.load_audio and the queue-time audio gate."""
import array
import math
import struct
import sys
import wave

import pytest


def test_load_audio_raw_f32(tmp_path):
    """Loading a .f32 file returns float32 samples, not a Python list."""
    from shesha.fm_audio import load_audio

    samples = [0.5, -0.3, 0.8]
    path = tmp_path / "test.f32"
    path.write_bytes(struct.pack(f'<{len(samples)}f', *samples))
    result = load_audio(str(path), 44100)
    assert isinstance(result, array.array)
    assert result.typecode == 'f'
    assert len(result) == 3
    assert list(result) == pytest.approx(samples)


def test_load_audio_rejects_too_many_samples(tmp_path):
    """Raw f32 sizes are checked before reading the file."""
    from shesha.fm_audio import AudioLoadError, load_audio

    path = tmp_path / "long.f32"
    path.write_bytes(struct.pack('<1000f', *([0.1] * 1000)))
    with pytest.raises(AudioLoadError, match='too long'):
        load_audio(str(path), 100, max_samples=500)


def test_load_audio_rejects_too_many_decoded_samples(tmp_path, monkeypatch):
    """The cap also applies to ffmpeg output, not just raw files."""
    import shesha.fm_audio as fm_audio

    path = tmp_path / "long.wav"
    path.write_bytes(b'x')

    class _Result:
        returncode = 0
        stdout = struct.pack('<1000f', *([0.1] * 1000))
        stderr = b''

    monkeypatch.setattr(
        fm_audio.subprocess, 'run', lambda *a, **k: _Result())

    with pytest.raises(fm_audio.AudioLoadError, match='too long'):
        fm_audio.load_audio(str(path), 100, max_samples=500)


def test_default_limit_allows_a_six_minute_track():
    """The guard must not reject a normal 6:24 track at 44.1 kHz."""
    from shesha.fm_audio import MAX_DECODED_SAMPLES

    assert MAX_DECODED_SAMPLES >= 44100 * 384


def test_load_audio_multisecond_wav(tmp_path):
    """A normal WAV decodes and prepares without a Python float list."""
    from shesha.fm_audio import load_audio, prepare_fm_audio

    rate = 44100
    n = rate * 2
    samples = array.array(
        'h', (int(0.5 * 32767 * math.sin(2.0 * math.pi * 440.0 * i / rate))
              for i in range(n)))
    if sys.byteorder != 'little':
        samples.byteswap()

    path = tmp_path / "tone.wav"
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())

    decoded = load_audio(str(path), rate)
    assert isinstance(decoded, array.array)
    assert decoded.typecode == 'f'
    assert len(decoded) == pytest.approx(n, abs=2)

    audio, _peak, _scale = prepare_fm_audio(decoded, rate)
    assert isinstance(audio, array.array)
    assert audio.typecode == 'f'
    assert max(abs(s) for s in audio) == pytest.approx(1.0)


def test_load_audio_missing_file():
    """Missing file raises with a readable reason."""
    from shesha.fm_audio import AudioLoadError, load_audio

    with pytest.raises(AudioLoadError):
        load_audio('/nonexistent/path.mp3', 44100)


def test_load_audio_ffmpeg_failure_reports_stderr(tmp_path, monkeypatch):
    """A decoder failure surfaces ffmpeg's own message, not a generic one."""
    import subprocess
    import shesha.fm_audio as fm_audio

    path = tmp_path / "broken.mp3"
    path.write_bytes(b'not really audio')

    class _Result:
        returncode = 1
        stdout = b''
        stderr = (f'{path}: Invalid data found when processing '
                  'input\n').encode()

    monkeypatch.setattr(
        fm_audio.subprocess, 'run', lambda *a, **k: _Result())

    with pytest.raises(fm_audio.AudioLoadError) as exc:
        fm_audio.load_audio(str(path), 44100)
    assert 'Invalid data found' in str(exc.value)


def test_load_audio_missing_ffmpeg(tmp_path, monkeypatch):
    """A missing ffmpeg binary is reported, not swallowed."""
    import shesha.fm_audio as fm_audio

    path = tmp_path / "song.mp3"
    path.write_bytes(b'x')

    def _raise(*a, **k):
        raise FileNotFoundError('ffmpeg')

    monkeypatch.setattr(fm_audio.subprocess, 'run', _raise)

    with pytest.raises(fm_audio.AudioLoadError) as exc:
        fm_audio.load_audio(str(path), 44100)
    assert 'ffmpeg' in str(exc.value)


def test_load_audio_no_audio_stream(tmp_path, monkeypatch):
    """A file ffmpeg decodes to no samples is rejected explicitly."""
    import shesha.fm_audio as fm_audio

    path = tmp_path / "movie.mp3"
    path.write_bytes(b'x')

    class _Result:
        returncode = 0
        stdout = b''
        stderr = b''

    monkeypatch.setattr(
        fm_audio.subprocess, 'run', lambda *a, **k: _Result())

    with pytest.raises(fm_audio.AudioLoadError) as exc:
        fm_audio.load_audio(str(path), 44100)
    assert 'no audio' in str(exc.value)


# ─── audio_file_error (queue-time gate) ──────────────────────────────


def test_audio_file_error_accepts_known_audio(tmp_path):
    from shesha.fm_audio import audio_file_error

    for name in ('song.mp3', 'track.FLAC', 'voice.wav', 'raw.f32'):
        path = tmp_path / name
        path.write_bytes(b'')
        assert audio_file_error(str(path)) is None


def test_audio_file_error_rejects_non_audio(tmp_path):
    from shesha.fm_audio import audio_file_error

    path = tmp_path / "notes.txt"
    path.write_bytes(b'hello')
    reason = audio_file_error(str(path))
    assert reason is not None
    assert 'not an audio file' in reason


def test_audio_file_error_rejects_missing(tmp_path):
    from shesha.fm_audio import audio_file_error

    reason = audio_file_error(str(tmp_path / 'gone.mp3'))
    assert reason is not None
    assert 'not a file' in reason


def test_audio_file_error_rejects_directory(tmp_path):
    from shesha.fm_audio import audio_file_error

    reason = audio_file_error(str(tmp_path))
    assert reason is not None


# ─── programme-audio level ───────────────────────────────────────────


def test_prepare_fm_audio_defaults_to_full_scale():
    """The client normalises to full scale; RDS headroom is the daemon's.

    The modulator applies the subcarrier headroom (fm_mod.c), so a live RDS
    toggle re-scales on air without a reload.  Pre-scaling here desynced the
    level from the subcarrier state.
    """
    from shesha.fm_audio import prepare_fm_audio

    raw = [0.3, -0.6, 0.9, -0.2] * 100
    audio, _peak, _scale = prepare_fm_audio(raw, 44100)
    assert max(abs(s) for s in audio) == pytest.approx(1.0)


def test_scale_to_peak_handles_negative_only_audio():
    """The fast peak scan must catch the most negative sample too."""
    from shesha.fm_audio import scale_to_peak

    scaled, scale = scale_to_peak(array.array('f', [-0.25, -0.5]))
    assert scale == pytest.approx(2.0)
    assert max(abs(s) for s in scaled) == pytest.approx(1.0)


def test_prepare_fm_audio_returns_array_and_preserves_scale():
    """Pre-emphasis and scaling keep float32 storage, not a float list."""
    from shesha.fm_audio import prepare_fm_audio

    raw = array.array('f', [0.3, -0.6, 0.9, -0.2] * 100)
    audio, peak, scale = prepare_fm_audio(raw, 44100)
    assert isinstance(audio, array.array)
    assert audio.typecode == 'f'
    assert peak > 0.0
    assert scale == pytest.approx(1.0 / peak)
    assert max(abs(s) for s in audio) == pytest.approx(1.0)
