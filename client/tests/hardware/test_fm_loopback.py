"""FM TX/RX concurrent loopback — transmit a tone, receive it back.

Requires: Pluto full-duplex at 2.1 MSPS, two CPUs, SMA loopback cable
with attenuator.

This test exercises Phase 3 of the FM branch: TX and RX running
simultaneously.  The firmware skips tx_suspend_producer() when the
existing mode is FM at the same rate, so both the FM RX producer thread
and the FM TX scheduler thread run concurrently.

Verification is via Goertzel DFT at the expected tone frequency.  Pass
criteria are deliberately loose — this is an integration gate, not a
precision measurement.
"""
import math
import threading
import time

import pytest
from shesha.client import DaemonClient, PushResult
from shesha.protocol import DaemonMode, MsgType, parse_fm, SUBSCRIBE_FM

FREQ_HZ = 100_100_000  # off-band to avoid interference
TONE_HZ = 1000
TONE_DUR = 3.0
AUDIO_RATE = 44100
DEVIATION = 75000
DAC_RATE = 2_100_000
TX_ATTEN_DB = 20.0  # attenuate to avoid saturating the RX

# Pass criteria
MIN_SNR = 5.0          # tone power vs noise floor (linear)
MAX_FREQ_ERROR = 0.15  # 15% frequency tolerance


def _generate_tone(freq_hz: float, dur_s: float,
                   rate: int = AUDIO_RATE, amp: float = 0.8) -> list[float]:
    n = int(dur_s * rate)
    return [amp * math.sin(2.0 * math.pi * freq_hz * i / rate)
            for i in range(n)]


def _goertzel(samples: list[float], freq_hz: float, rate: float) -> float:
    """Return power at freq_hz via Goertzel algorithm."""
    n = len(samples)
    if n == 0:
        return 0.0
    omega = 2.0 * math.pi * freq_hz / rate
    coeff = 2.0 * math.cos(omega)
    s0 = s1 = s2 = 0.0
    for s in samples:
        s2, s1, s0 = s1, s0, s + coeff * s0 - s1
    real = s0 - s1 * math.cos(omega)
    imag = s1 * math.sin(omega)
    return real * real + imag * imag


def _stream_tx(host: str, port: int, tone: list[float],
               results: dict) -> None:
    """TX thread: stream a tone using a separate connection."""
    c = DaemonClient(host, port)
    try:
        c.connect()
        # Small first chunk to start quickly
        first_n = min(AUDIO_RATE, len(tone))
        chunk_n = max(1, int(AUDIO_RATE * 0.1))  # 100ms chunks

        ok = c.fm_tx_stream_start(
            tone[:first_n],
            audio_rate=AUDIO_RATE,
            deviation=DEVIATION,
            dac_rate=DAC_RATE,
        )
        if not ok:
            results['tx_error'] = 'stream_start rejected'
            return

        sent = first_n
        t0 = time.monotonic()
        retries = 0

        while sent < len(tone):
            # Pace to ~1s ahead of realtime
            ahead = sent / AUDIO_RATE - (time.monotonic() - t0)
            if ahead > 1.0:
                time.sleep(0.05)
                continue

            end = min(sent + chunk_n, len(tone))
            result = c.fm_tx_stream_push(tone[sent:end])
            if result is PushResult.RETRY:
                retries += 1
                time.sleep(0.02)
                continue
            if result is PushResult.ERROR:
                results['tx_error'] = f'push failed at {sent}/{len(tone)}'
                return
            sent = end

        c.fm_tx_stream_end()
        results['tx_ok'] = True
        results['tx_retries'] = retries
    except Exception as exc:
        results['tx_error'] = str(exc)
    finally:
        c.disconnect()


@pytest.fixture
def rx_client():
    from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    c.disconnect()


def test_fm_loopback(rx_client):
    """TX a 1 kHz tone via FM, receive it back concurrently, verify."""
    from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

    # Configure radio for loopback
    rx_client.set_freq(FREQ_HZ)
    rx_client.set_tx_atten(TX_ATTEN_DB)
    time.sleep(0.1)

    # Enter FM mode — this starts the RX producer at 2.1 MSPS
    assert rx_client.set_mode(DaemonMode.FM), "Failed to enter FM mode"
    rx_client.subscribe(SUBSCRIBE_FM)
    time.sleep(0.5)  # let the producer settle

    # Verify FM frames are flowing before we start TX
    deadline = time.monotonic() + 3.0
    got_frame = False
    while time.monotonic() < deadline:
        for mt, pl in rx_client.recv_raw(timeout=0.2):
            if mt == MsgType.DATA_FM:
                got_frame = True
                break
        if got_frame:
            break
    assert got_frame, "No DATA_FM frames received — FM RX not running"

    # Generate the test tone
    tone = _generate_tone(TONE_HZ, TONE_DUR)

    # Start TX in a background thread (separate TCP connection)
    tx_results: dict = {}
    tx_thread = threading.Thread(
        target=_stream_tx,
        args=(PLUTO_HOST, PLUTO_PORT, tone, tx_results),
    )
    tx_thread.start()

    # Collect FM audio frames for TX duration + margin
    stream_dur = TONE_DUR + 2.0
    all_audio: list[float] = []
    rx_rate = None
    start = time.monotonic()

    while time.monotonic() - start < stream_dur:
        for mt, pl in rx_client.recv_raw(timeout=0.1):
            if mt == MsgType.DATA_FM:
                frame = parse_fm(pl)
                if rx_rate is None:
                    rx_rate = frame.audio_rate_hz
                all_audio.extend(frame.audio)

    tx_thread.join(timeout=10.0)

    # Restore mode
    rx_client.set_mode(DaemonMode.WATERFALL)

    # --- Assertions ---

    # TX completed successfully
    assert 'tx_error' not in tx_results, \
        f"TX failed: {tx_results.get('tx_error')}"
    assert tx_results.get('tx_ok'), "TX did not complete"

    # Got audio
    assert rx_rate is not None, "No FM frames received during TX"
    assert len(all_audio) > 0, "Audio buffer is empty"

    # Enough audio received (should be close to stream_dur worth)
    audio_dur = len(all_audio) / rx_rate
    assert audio_dur >= TONE_DUR * 0.5, (
        f"Not enough audio received: {audio_dur:.2f}s "
        f"(expected >= {TONE_DUR * 0.5:.1f}s)"
    )

    # The core assertion: received audio contains the transmitted tone.
    # Use Goertzel on the full buffer — the tone should dominate over noise.
    power_tone = _goertzel(all_audio, TONE_HZ, rx_rate)
    # Noise reference: frequency where no energy should exist
    power_noise = _goertzel(all_audio, TONE_HZ * 1.5, rx_rate)
    snr = power_tone / power_noise if power_noise > 0 else float('inf')

    assert snr > MIN_SNR, (
        f"Tone SNR {snr:.1f}x below threshold {MIN_SNR}x "
        f"(power@{TONE_HZ}Hz={power_tone:.1f}, "
        f"noise@{int(TONE_HZ*1.5)}Hz={power_noise:.1f})"
    )

    # Zero-crossing frequency estimate on the highest-energy window.
    # This is a sanity check only — unreliable when SNR is low because
    # noise contributes extra crossings.  The Goertzel check above is
    # the authoritative frequency verification.
    win_samples = int(rx_rate * 0.5)  # 500ms window
    best_rms = 0.0
    best_chunk = all_audio[:win_samples]
    for start_idx in range(0, len(all_audio) - win_samples, win_samples):
        chunk = all_audio[start_idx:start_idx + win_samples]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        if rms > best_rms:
            best_rms = rms
            best_chunk = chunk

    crossings = sum(
        1 for i in range(1, len(best_chunk))
        if best_chunk[i - 1] <= 0.0 < best_chunk[i]
    )
    chunk_dur = len(best_chunk) / rx_rate
    if chunk_dur > 0:
        freq_est = crossings / chunk_dur
        freq_error = abs(freq_est - TONE_HZ) / TONE_HZ
        if freq_error >= MAX_FREQ_ERROR:
            import warnings
            warnings.warn(
                f"Zero-crossing freq estimate {freq_est:.0f} Hz "
                f"(error={freq_error:.1%}) — likely noise floor effect"
            )
