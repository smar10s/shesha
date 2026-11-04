"""Audio playback helpers shared by the CLI and TUI.

Zero Python dependencies.  Playback shells out to a local player that reads
raw f32le mono on stdin (ffplay, sox ``play``, or ``aplay``), fed from a
bounded queue by a daemon feeder thread — off the receive path so a full
queue drops samples rather than backpressuring the RX loop.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading

from .protocol import pack_f32le


# ffplay gained -ch_layout in 5.1 and dropped -ac in 7.0, so neither
# option spans the installed range. Pick by version.
_FFPLAY_CH_LAYOUT_VERSION = (5, 1)

# Players without an exposed clock cannot say where playback is.  Delay the
# waveform by this much instead: it is deterministic, not exact.
_FALLBACK_DELAY_SEC = 0.30

# ffplay's status line: "   0.07 M-A:  0.000 fd= ... aq= ...".  The first
# field is the audio clock; the rest is buffer state.  ``nan`` before start
# and split reads are ignored.
_FFPLAY_CLOCK_RE = re.compile(r'(-?\d+\.\d+)\s+(?:M-A|A-V):')


def _parse_status_clock(text: str) -> float | None:
    """Latest complete ffplay audio clock in a status buffer, or None."""
    matches = _FFPLAY_CLOCK_RE.findall(text)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _ffplay_channel_args(version_text: str) -> list[str]:
    """Channel option for raw mono input, keyed to the ffplay version."""
    match = re.search(r'ffplay version (\d+)\.(\d+)', version_text)
    if match:
        version = (int(match.group(1)), int(match.group(2)))
        if version >= _FFPLAY_CH_LAYOUT_VERSION:
            return ['-ch_layout', 'mono']
    return ['-ac', '1']


def _ffplay_version() -> str:
    """Version banner of the installed ffplay, or '' if it will not run."""
    try:
        result = subprocess.run(['ffplay', '-version'], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout


def find_player(rate_hz: int) -> tuple[list[str] | None, str | None]:
    """Find an audio player that accepts raw f32le on stdin.

    Returns (argv, label), or (None, None) if no player is installed.
    """
    if shutil.which('ffplay'):
        return ([
            'ffplay', '-hide_banner', '-loglevel', 'error',
            '-nodisp', '-autoexit', '-stats',
            '-fflags', 'nobuffer',
            '-analyzeduration', '0', '-probesize', '32',
            '-f', 'f32le', '-ar', str(rate_hz),
            *_ffplay_channel_args(_ffplay_version()),
            '-i', 'pipe:0',
        ], 'ffplay')
    if shutil.which('play'):
        return ([
            'play', '-q', '-t', 'raw', '-e', 'float', '-b', '32',
            '-r', str(rate_hz), '-c', '1', '-',
        ], 'sox play')
    if shutil.which('aplay'):
        return ([
            'aplay', '-q', '-f', 'FLOAT_LE', '-r', str(rate_hz), '-c', '1',
        ], 'aplay')
    return (None, None)


class AudioPlayer:
    """A spawned local player fed f32le mono audio.

    Feed with :meth:`push` (non-blocking; drops on backpressure and counts
    the drops).  Check ``player_gone`` to detect a player that exited early.
    Call :meth:`close` to tear down the feeder thread and process.
    """

    def __init__(self, rate_hz: int) -> None:
        self.rate = rate_hz
        argv, self.label = find_player(rate_hz)
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue | None = None
        self._feeder: threading.Thread | None = None
        self._clock_thread: threading.Thread | None = None
        self._clock_seconds: float | None = None
        self.accepted: int = 0
        self.dropped: int = 0
        self.player_gone: bool = False
        if argv is None:
            return
        # stdout must not inherit the terminal: under the TUI the player
        # would splatter its diagnostics over the alt screen.  ffplay's
        # status goes to stderr and is parsed for its audio clock, so that
        # stream is a pipe rather than DEVNULL.
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if self.label == 'ffplay'
            else subprocess.DEVNULL)
        self._q = queue.Queue(maxsize=16)
        self._feeder = threading.Thread(
            target=self._feed, args=(self._q,), daemon=True)
        self._feeder.start()
        if self.label == 'ffplay':
            self._clock_thread = threading.Thread(
                target=self._read_clock, daemon=True)
            self._clock_thread.start()

    @property
    def available(self) -> bool:
        """True if a player process was actually spawned."""
        return self._proc is not None

    def _feed(self, q: queue.Queue) -> None:
        while True:
            buf = q.get()
            if buf is None:
                break
            try:
                self._proc.stdin.write(buf)
            except (BrokenPipeError, OSError, ValueError):
                self.player_gone = True
                break

    def _read_clock(self) -> None:
        """Track ffplay's audio clock from its status lines."""
        stream = self._proc.stderr if self._proc is not None else None
        if stream is None:
            return
        buf = ''
        while True:
            try:
                # read1 returns what is available.  read() would block until
                # the pipe fills, which a status line never does.
                chunk = stream.read1(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if isinstance(chunk, bytes):
                chunk = chunk.decode('ascii', 'ignore')
            buf = (buf + chunk)[-4096:]
            clock = _parse_status_clock(buf)
            if clock is not None:
                self._clock_seconds = clock

    def played_samples(self) -> int | None:
        """Samples audible so far, or None when no clock is available.

        ffplay reports its own audio clock, so this tracks the device.  The
        other players expose nothing; fall back to a fixed delay from the
        accepted-sample frontier.
        """
        if self._clock_seconds is not None:
            return max(0, int(self._clock_seconds * self.rate))
        if self.label == 'ffplay':
            return None
        return max(0, self.accepted - int(_FALLBACK_DELAY_SEC * self.rate))

    def push(self, audio: list[float]) -> bool:
        """Queue a chunk of mono float samples.  False means it was dropped."""
        q = self._q
        if q is None:
            return False
        try:
            q.put_nowait(pack_f32le(audio))
        except queue.Full:
            self.dropped += 1
            return False
        self.accepted += len(audio)
        return True

    def close(self) -> None:
        """Stop feeding, join the feeder thread, and reap the process."""
        if self._q is not None:
            try:
                self._q.put(None, timeout=1.0)
            except queue.Full:
                pass
        if self._feeder is not None:
            self._feeder.join(timeout=3.0)
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except (OSError, BrokenPipeError, ValueError):
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._clock_thread is not None:
            self._clock_thread.join(timeout=3.0)
            self._clock_thread = None
        if self._proc is not None and self._proc.stderr is not None:
            try:
                self._proc.stderr.close()
            except (OSError, ValueError):
                pass
        self._proc = None
        self._q = None
        self._feeder = None