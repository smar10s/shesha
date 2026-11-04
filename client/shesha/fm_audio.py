"""FM audio conditioning, client side.

Pre-emphasis lives here rather than in the daemon for two reasons.

Correctness: 75 us pre-emphasis boosts 15 kHz by about 17 dB, so the peak
of the filtered signal cannot be bounded from a streaming window.  Audio
above 1.0 does not clip IQ — |IQ| = 1 for FM — it clips *deviation*, and
that reaches the receiver as overdeviation, adjacent-channel splatter and
distortion after de-emphasis.  The client holds the whole file, so it can
filter once with continuous state, find the true global peak, and scale
exactly.

Cost: the Pluto exposes a single CPU core that the poll loop, the producer
thread and FM modulation already contend for.  The client has a laptop.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

from array import array
from collections.abc import Sequence

PREEMPHASIS_US = 75

# Bound the decoded track before it becomes an unrecoverable allocation.
# 40M float32 samples is about 160 MB, or 15 minutes at 44.1 kHz, which
# leaves a normal 6:24 track well inside the limit.
MAX_DECODED_SAMPLES = 40_000_000

# Elementwise writes to array('f') are slow in CPython.  Scaling a bounded
# list block and assigning it back keeps peak memory small and runs faster
# than assigning array elements one at a time.
_PROCESS_CHUNK = 65_536

_log = logging.getLogger(__name__)


class AudioLoadError(RuntimeError):
    """An audio file could not be decoded; the message carries the reason."""


# Extensions ffmpeg is expected to decode into audio.  Used to reject
# obviously non-audio files at queue time, before a decode is ever tried.
AUDIO_EXTENSIONS = frozenset({
    '.aac', '.aif', '.aiff', '.alac', '.ape', '.flac', '.m4a', '.m4b',
    '.mka', '.mp2', '.mp3', '.mpc', '.oga', '.ogg', '.opus', '.wav',
    '.wma', '.wv',
})

# Headerless raw mono float32, read directly rather than through ffmpeg.
RAW_EXTENSIONS = frozenset({'.pcm', '.f32', '.raw'})

SUPPORTED_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | RAW_EXTENSIONS


def audio_file_error(path: str) -> str | None:
    """Why *path* cannot be queued as an audio track, or None if it can.

    This is the cheap queue-time gate: it never decodes.  A file with a
    supported extension that turns out to be corrupt or mislabelled still
    fails later in :func:`load_audio`, which now reports the real reason.
    """
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return f'not a file: {name or path}'
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_AUDIO_EXTENSIONS:
        return f'not an audio file: {name}'
    return None


def _first_error_line(text: str) -> str:
    """First non-empty line of decoder output, for a one-line status.

    ffmpeg prefixes many lines with ``[codec @ 0xADDR]``; that context is
    noise in a status bar, so strip it.
    """
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r'^\[[^\]]*\]\s*', '', line).strip() or line
    return ''


def _as_f32(audio: Sequence[float]) -> array:
    """Return float32 storage, reusing an existing float32 array."""
    if isinstance(audio, array) and audio.typecode == 'f':
        return audio
    return array('f', audio)


def _peak(samples: Sequence[float]) -> float:
    """Largest absolute sample, using the fast C max/min iterators."""
    return max(max(samples, default=0.0), -min(samples, default=0.0))


def _scale_in_place(samples: array, scale: float) -> None:
    """Multiply every sample in place, using bounded list blocks."""
    n = len(samples)
    for start in range(0, n, _PROCESS_CHUNK):
        end = min(start + _PROCESS_CHUNK, n)
        block = samples[start:end].tolist()
        samples[start:end] = array('f', [s * scale for s in block])


def preemphasis(audio: Sequence[float], sample_rate: int,
                tau_us: int = PREEMPHASIS_US) -> array:
    """Apply a first-order pre-emphasis filter, exact inverse of the RX
    de-emphasis in fm_rx.c.

    y[i] = gain * x[i] - feedback * x[i-1]

    Seeded with x[-1] = x[0], which makes y[0] == x[0] identically:
    gain - feedback = 1/alpha - (1-alpha)/alpha = 1.  No startup transient.

    Not normalised — call scale_to_peak() next.  Kept separate so the
    filter and the level policy can be reasoned about independently.

    A float32 array is filtered in place; any other sequence is copied
    first.  In-place filtering avoids a second full-track allocation.
    """
    n = len(audio)
    out = _as_f32(audio)
    if n < 2:
        return out

    dt = 1.0 / sample_rate
    tau = tau_us * 1e-6
    alpha = dt / (dt + tau)
    gain = 1.0 / alpha
    feedback = (1.0 - alpha) / alpha

    prev = out[0]
    for i in range(n):
        x = out[i]
        out[i] = gain * x - feedback * prev
        prev = x
    return out


def scale_to_peak(audio: Sequence[float],
                  target: float = 1.0) -> tuple[array, float]:
    """Scale so the largest absolute sample equals `target`.

    Deterministic in both directions, because deviation is proportional to
    amplitude: with target 1.0 the transmitted peak deviation is exactly
    the configured figure, which is what makes a measurement repeatable.

    Near-silent input is passed through unchanged rather than amplified
    into noise.

    A float32 array is scaled in place; any other sequence is copied
    first.  Returns (scaled, scale_applied).
    """
    out = _as_f32(audio)
    peak = _peak(out)
    if peak < 1e-6:
        return out, 1.0
    scale = target / peak
    _scale_in_place(out, scale)
    return out, scale


def prepare_fm_audio(audio: Sequence[float], sample_rate: int,
                     peak: float = 1.0,
                     tau_us: int = PREEMPHASIS_US
                     ) -> tuple[array, float, float]:
    """Pre-emphasise then scale.  Returns (audio, preemph_peak, scale)."""
    filtered = preemphasis(audio, sample_rate, tau_us)
    raw_peak = _peak(filtered)
    scaled, scale = scale_to_peak(filtered, peak)
    return scaled, raw_peak, scale


def _too_long_error(path: str, n: int, max_samples: int,
                    target_rate: int) -> AudioLoadError:
    rate = max(1, target_rate)
    return AudioLoadError(
        f'{os.path.basename(path)}: audio too long '
        f'({n / rate:.1f}s, {n} samples; '
        f'max {max_samples / rate:.1f}s, {max_samples} samples)')


def _clip_peak_in_place(samples: array, peak: float) -> float:
    """Scale samples above full scale down, returning the new peak."""
    if peak <= 1.0:
        return peak
    _scale_in_place(samples, 1.0 / peak)
    return 1.0


def load_audio(path: str, target_rate: int, *,
               max_samples: int = MAX_DECODED_SAMPLES) -> array:
    """Decode an audio file to mono float32 samples at target_rate.

    Uses ffmpeg for most formats, reads raw f32 LE directly for
    .pcm/.f32/.raw files.  Returns ``array('f')``, never a Python list, so
    a normal track costs four bytes per sample.

    Raises :class:`AudioLoadError` with the decoder's own message on
    failure, or a "too long" message when the decoded sample count exceeds
    *max_samples*.  Callers can lower that bound for tests or policy.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in RAW_EXTENSIONS:
        try:
            n = os.path.getsize(path) // 4
        except OSError as exc:
            raise AudioLoadError(
                f'cannot read {os.path.basename(path)}: {exc}') from exc
        if n > max_samples:
            raise _too_long_error(path, n, max_samples, target_rate)
        try:
            with open(path, 'rb') as f:
                samples = array('f')
                samples.fromfile(f, n)
        except (OSError, EOFError) as exc:
            raise AudioLoadError(
                f'cannot read {os.path.basename(path)}: {exc}') from exc
        if sys.byteorder != 'little':
            samples.byteswap()
        if not samples:
            raise AudioLoadError(f'no audio in {os.path.basename(path)}')
        peak = _peak(samples)
        peak = _clip_peak_in_place(samples, peak)
        _log.info("loaded %d f32 samples from %s, peak=%.3f", n, path, peak)
        return samples

    # A long track on a slow host must not hit a fixed 30 s wall.  Scale
    # the decoder budget with the configured sample cap instead.
    timeout = max(30.0, 2.0 * max_samples / max(1, target_rate))
    try:
        result = subprocess.run(
            ['ffmpeg', '-i', path, '-ac', '1', '-ar', str(target_rate),
             '-f', 'f32le', '-hide_banner', '-loglevel', 'error',
             'pipe:1'],
            capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        _log.error("ffmpeg not found — install ffmpeg or use .f32 raw PCM")
        raise AudioLoadError(
            'ffmpeg not found — install ffmpeg or use .f32 raw PCM') from exc
    except subprocess.TimeoutExpired as exc:
        _log.error("ffmpeg timed out decoding %s", path)
        raise AudioLoadError(
            f'ffmpeg timed out decoding {os.path.basename(path)}') from exc

    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')
        msg = _first_error_line(stderr)
        prefix = f'{path}:'
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
        _log.error("ffmpeg decode failed for %s: %s", path, msg or stderr.strip())
        raise AudioLoadError(msg or f'cannot decode {os.path.basename(path)}')

    n = len(result.stdout) // 4
    if n > max_samples:
        raise _too_long_error(path, n, max_samples, target_rate)
    samples = array('f')
    samples.frombytes(result.stdout[:n * 4])
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        _log.error("ffmpeg produced no audio for %s", path)
        raise AudioLoadError(f'no audio stream in {os.path.basename(path)}')
    peak = _peak(samples)
    peak = _clip_peak_in_place(samples, peak)
    _log.info("decoded %d samples (%.1fs) from %s, peak=%.3f",
               n, n / target_rate, path, peak)
    return samples
