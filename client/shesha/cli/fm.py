from __future__ import annotations

import argparse
import array
import sys
import time
import wave

from shesha.audio import AudioPlayer
from shesha.client import DaemonClient, PushResult
from shesha.fm_audio import prepare_fm_audio
from shesha.protocol import (
    DaemonMode, FrameDecodeError, MAX_FM_AUDIO_RATE_HZ, MsgType, parse_fm,
    SUBSCRIBE_FM,
)
from shesha.rds import pty_name, pty_text, ps_text

from .common import (
    RadioState, _connect, check, fail, print_json, resolve_freq,
    restore_radio, snapshot_radio,
)


# FM streaming pacing.  FM_LEAD_SEC is how far ahead of realtime playout the
# client may run: enough to ride out network and scheduling jitter, small
# enough that daemon memory and TX latency stay bounded.  Chunks are short so
# the pacing loop reacts quickly.
FM_LEAD_SEC = 1.0
FM_CHUNK_SEC = 0.1


def _pcm16(audio: list[float]) -> bytes:
    """Convert demodulated audio to 16-bit PCM, removing per-chunk DC."""
    if not audio:
        return b""
    mean = sum(audio) / len(audio)
    samples = array.array('h', (
        int(max(-1.0, min(1.0, sample - mean)) * 32767)
        for sample in audio))
    return samples.tobytes()


def _fm_summary(freq_mhz: float, rate: int | None, n_frames: int,
                n_samples: int, elapsed: float, dropped: int,
                wav: str | None, decode_errors: int = 0) -> dict:
    audio_s = n_samples / rate if rate else 0.0
    return {
        "freq_mhz": freq_mhz,
        "audio_rate_hz": rate,
        "frames": n_frames,
        "samples": n_samples,
        "audio_s": round(audio_s, 3),
        "wall_s": round(elapsed, 3),
        "realtime_pct": (round(100.0 * audio_s / elapsed, 2)
                         if elapsed > 0 else None),
        "dropped_frames": dropped,
        "decode_errors": decode_errors,
        "wav": wav,
    }


def _fm_report(c: DaemonClient, elapsed: float, sent_s: float,
               total_s: float) -> None:
    ts = c.tx_stats()
    if ts is None:
        return
    print(f"  t={elapsed:5.1f}s  sent {sent_s:5.1f}/{total_s:.1f}s  "
          f"out {ts.rate_frac * 100:6.2f}% nominal  "
          f"ddr {ts.depth_ms:5.1f}ms  ring {ts.ring_fill_ms / 1000:4.2f}s  "
          f"underruns {ts.underruns}", file=sys.stderr)


# ---------------------------------------------------------------------------
# fm tx: streaming FM transmitter
# ---------------------------------------------------------------------------


def do_fm_tx(args: argparse.Namespace) -> None:
    """Stream audio over FM TX with real-time pacing.

    Reads from a file (any format ffmpeg can decode) or raw f32le on stdin.
    Streams in chunks with backpressure-aware pacing so daemon memory stays
    bounded.  Reports FPGA telemetry at intervals.
    """
    freq_mhz = resolve_freq(args)
    freq_hz = int(freq_mhz * 1e6)
    if args.rate <= 0:
        fail("--rate must be positive")
    if args.rate > MAX_FM_AUDIO_RATE_HZ:
        fail(f"--rate must be at most {MAX_FM_AUDIO_RATE_HZ} Hz")

    # Load audio from file or stdin
    if args.file == '-':
        raw_bytes = sys.stdin.buffer.read()
        n = len(raw_bytes) // 4
        if n < 2:
            fail("stdin: need at least 2 float32 samples")
        raw = array.array('f')
        raw.frombytes(raw_bytes[:n * 4])
        if sys.byteorder != 'little':
            raw.byteswap()
        print(f"Read {n} samples ({n / args.rate:.1f}s) from stdin", file=sys.stderr)
    else:
        raw = _load_audio(args.file, args.rate)

    if len(raw) < 2:
        fail("audio too short")

    # Pre-emphasise and scale
    audio, preemph_peak, scale = prepare_fm_audio(raw, args.rate, peak=1.0)

    dev_hz = 75_000
    dac_hz = 2_100_000
    total = len(audio)
    duration_s = total / args.rate

    # First chunk covers prefill (~1 s); subsequent chunks are small
    first_n = min(args.rate, total)
    chunk_n = max(1, int(args.rate * FM_CHUNK_SEC))

    c = _connect(args)
    state = snapshot_radio(c)
    try:
        check(c, c.set_freq(freq_hz), "could not set frequency")
        if args.tx_atten is not None:
            check(c, c.set_tx_atten(args.tx_atten),
                  "could not set TX attenuation")

        check(c, c.fm_tx_stream_start(
                  audio[:first_n], audio_rate=args.rate,
                  deviation=dev_hz, dac_rate=dac_hz),
              "could not start FM stream")

        t0 = time.monotonic()
        print(f"TX {duration_s:.1f}s at {freq_mhz:.3f} MHz "
              f"(pre-emphasis peak {preemph_peak:.2f}, scale {scale:.3f})",
              file=sys.stderr)

        sent = first_n
        retries = 0
        next_report = t0 + 2.0

        while sent < total:
            ahead = sent / args.rate - (time.monotonic() - t0)
            if ahead > FM_LEAD_SEC:
                time.sleep(min(0.05, ahead - FM_LEAD_SEC))
                continue

            end = min(sent + chunk_n, total)
            result = c.fm_tx_stream_push(audio[sent:end])
            if result is PushResult.RETRY:
                retries += 1
                time.sleep(0.05)
                continue
            if result is PushResult.ERROR:
                detail = c.last_error
                c.fm_tx_stream_end()
                fail(f"stream push failed at {sent}/{total}"
                     + (f": {detail}" if detail else ""))
            sent = end

            now = time.monotonic()
            if now >= next_report:
                _fm_report(c, now - t0, sent / args.rate, duration_s)
                next_report = now + 2.0

        check(c, c.fm_tx_stream_end(), "could not end FM stream")

        # Wait for playout to drain
        deadline = time.monotonic() + duration_s + 30
        while time.monotonic() < deadline:
            ts = c.tx_stats()
            if ts is not None and not ts.tx_active:
                break
            now = time.monotonic()
            if now >= next_report:
                _fm_report(c, now - t0, sent / args.rate, duration_s)
                next_report = now + 2.0
            time.sleep(0.2)

        ts = c.tx_stats()
        out = {"freq_mhz": freq_mhz, "audio_rate": args.rate,
               "deviation_khz": 75.0,
               "dac_rate_khz": 2100.0,
               "samples": total,
               "duration_s": round(duration_s, 2),
               "peak_scale": round(scale, 4),
               "push_retries": retries}
        if ts is not None:
            out.update({
                "output_rate_pct": round(ts.rate_frac * 100.0, 3),
                "underruns": ts.underruns,
                "starved_samples": ts.starved_samples,
            })
        if args.json:
            print_json(out)
        else:
            print(f"TX complete: {duration_s:.1f} s at {freq_mhz:.3f} MHz")
            if ts is not None:
                print(f"output: {ts.rate_frac * 100:.2f}% nominal, "
                      f"{ts.underruns} underruns")
    except KeyboardInterrupt:
        # Ctrl-C is the normal way to stop a long stream.  Send an
        # explicit stop rather than just dropping the socket: the daemon
        # would otherwise keep radiating pad samples from the stream.
        try:
            c.tx_stop()
        except Exception:
            pass
        print("interrupted — TX stopped", file=sys.stderr)
    finally:
        restore_radio(c, state)
        c.disconnect()


# ---------------------------------------------------------------------------
# fm rx: live FM receiver
# ---------------------------------------------------------------------------


def do_fm_rx(args: argparse.Namespace) -> None:
    """Live FM receiver: tune, demodulate, play audio.

    Enters FM mode on the daemon, receives DATA_FM frames, and pipes the
    demodulated audio to a local player (ffplay/sox/aplay).  Optionally
    records to WAV.  Ctrl-C to stop.

    The audio rate is read from the first received frame (currently 43750 Hz)
    rather than assumed, because it is a property of the firmware decimator
    chain.
    """
    freq_mhz = resolve_freq(args)
    freq_hz = int(freq_mhz * 1e6)

    c = _connect(args)
    state = None
    player = None
    wav_file = None
    rate = None
    summary = None
    start = 0.0
    n_frames = 0
    n_samples = 0
    dropped = 0
    decode_errors = 0
    elapsed = 0.0

    try:
        st, prev_mode = c.status() or (None, None)
        if st:
            state = RadioState.from_status(st, prev_mode)

        check(c, c.set_freq(freq_hz), "could not set frequency")

        # Switch first: the previous mode may lock the AGC or gain.
        check(c, c.set_mode(DaemonMode.FM), "could not switch to FM mode")
        if args.gain is not None:
            check(c, c.set_agc(0), "could not switch AGC to manual")
            check(c, c.set_gain(args.gain), "could not set gain")
        check(c, c.subscribe(SUBSCRIBE_FM), "could not subscribe to FM data")

        # Wait for first frame to learn the delivered audio rate
        print(f"Tuning {freq_mhz:.1f} MHz, waiting for audio...",
              file=sys.stderr)
        deadline = time.monotonic() + 5.0
        while rate is None and time.monotonic() < deadline:
            if not c.connected:
                fail("connection lost while waiting for FM data")
            for mt, pl in c.recv_raw(timeout=0.2):
                if mt == MsgType.DATA_FM:
                    try:
                        rate = parse_fm(pl).audio_rate_hz
                    except FrameDecodeError as exc:
                        decode_errors += 1
                        print(f"skipping bad FM frame: {exc}",
                              file=sys.stderr)
                        continue
                    break
        if rate is None:
            fail("no DATA_FM frames — is the daemon in FM mode?")

        # Setup player
        player = AudioPlayer(rate)
        if not player.available:
            print("No audio player found (need ffplay, sox play, or aplay).\n"
                  "Use --wav to record instead.", file=sys.stderr)
            if not args.wav:
                fail("no audio player available")
        else:
            print(f"Playing via {player.label} at {rate} Hz — Ctrl-C to stop",
                  file=sys.stderr)

        if args.wav:
            try:
                wav_file = wave.open(args.wav, 'w')
            except OSError as exc:
                fail(f"cannot write {args.wav}: {exc}")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(rate)

        # Drain any frames that accumulated during mode switch + FIR load.
        # Without this, the player starts with stale audio from seconds ago.
        c.recv_raw(timeout=0.0)

        # Main receive loop
        start = time.monotonic()

        while True:
            if not c.connected:
                break
            if args.duration and (time.monotonic() - start) >= args.duration:
                break
            if player is not None and player.player_gone:
                print("\nPlayer exited", file=sys.stderr)
                break
            for mt, pl in c.recv_raw(timeout=0.1):
                if mt != MsgType.DATA_FM:
                    continue
                try:
                    audio = parse_fm(pl).audio
                except FrameDecodeError as exc:
                    decode_errors += 1
                    print(f"skipping bad FM frame: {exc}", file=sys.stderr)
                    continue
                n_frames += 1
                n_samples += len(audio)
                if args.volume != 1.0:
                    audio = [s * args.volume for s in audio]
                if player is not None:
                    player.push(audio)
                if wav_file:
                    wav_file.writeframes(_pcm16(audio))

        dropped = player.dropped if player is not None else 0
        elapsed = time.monotonic() - start
        summary = _fm_summary(
            freq_mhz, rate, n_frames, n_samples, elapsed, dropped, args.wav,
            decode_errors)
        realtime = summary["realtime_pct"]
        realtime_text = f"{realtime:.1f}%" if realtime is not None else "?"
        print(f"\n{n_frames} frames, {n_samples} samples, "
              f"{summary['audio_s']:.2f}s audio in {elapsed:.2f}s wall "
              f"({realtime_text} realtime)",
              file=sys.stderr)
        if dropped:
            print(f"  {dropped} frame(s) dropped (player backpressure)",
                  file=sys.stderr)
        if decode_errors:
            print(f"  {decode_errors} frame(s) skipped (bad decode)",
                  file=sys.stderr)

    except KeyboardInterrupt:
        if rate is not None:
            elapsed = time.monotonic() - start
            summary = _fm_summary(
                freq_mhz, rate, n_frames, n_samples, elapsed, dropped,
                args.wav, decode_errors)
        print("\nStopped", file=sys.stderr)
    finally:
        if player is not None:
            player.close()
        if wav_file:
            wav_file.close()
            print(f"Wrote {args.wav}", file=sys.stderr)
        if getattr(args, "json", False) and summary is not None:
            print_json(summary)
        restore_radio(c, state)
        c.disconnect()


# ---------------------------------------------------------------------------
# rds: monitor decoded RDS state on an FM station
# ---------------------------------------------------------------------------


def _rds_line(s, freq_mhz: float) -> str:
    pi = s.pi_text if s.pi_valid else '----'
    ps = ps_text(s.ps, s.ps_mask)
    pty = (pty_text(s.pty) if s.pi_valid else '') or '---'
    rt = s.rt.rstrip() if s.rt_valid else ''
    flags = 'T' if s.tp else '-'
    flags += 'A' if s.ta else '-'
    return (f'{freq_mhz:5.1f} MHz  PI {pi}  PS "{ps}"  PTY {pty}  '
            f'[{flags}]  RT "{rt}"')


def _rds_json(s, freq_mhz: float) -> dict:
    return {
        'freq_mhz': freq_mhz,
        'pi': s.pi, 'pi_hex': s.pi_text, 'pi_valid': s.pi_valid,
        'ps': s.ps, 'ps_complete': s.ps_complete,
        'pty': s.pty, 'pty_name': pty_name(s.pty) if s.pty else None,
        'tp': s.tp, 'ta': s.ta, 'ms': s.ms,
        'rt': s.rt.rstrip(), 'rt_valid': s.rt_valid,
        'ptyn': s.ptyn, 'ptyn_complete': s.ptyn_complete,
        'af': s.af,
        'ct_valid': s.ct_valid, 'ct_mjd': s.ct_mjd,
        'ct_minutes': s.ct_minutes,
        'ct_offset_half_hours': s.ct_offset_half_hours,
        'oda': [{'aid': f'{aid:04X}', 'len': n, 'data': data.hex()}
                for aid, n, data in s.oda],
        'synced': s.synced, 'groups': s.groups, 'blocks': s.blocks,
        'block_errors': s.block_errors, 'sync_losses': s.sync_losses,
    }


def do_rds(args: argparse.Namespace) -> None:
    """Tune an FM station and print decoded RDS state as it fills in.

    The daemon does the decoding; this command is the hardware-validation
    path that proves a real station's PI/PS/RT arrive intact. With --json
    the final state is printed as one object, which is what a script wants.
    """
    freq_mhz = resolve_freq(args)
    freq_hz = int(freq_mhz * 1e6)

    c = _connect(args)
    state = None
    last_line = None
    final = None
    json_printed = False
    decode_errors = 0

    try:
        st, prev_mode = c.status() or (None, None)
        if st:
            state = RadioState.from_status(st, prev_mode)

        check(c, c.set_freq(freq_hz), "could not set frequency")

        # Switch first: the previous mode may lock the AGC or gain.
        check(c, c.set_mode(DaemonMode.FM), "could not switch to FM mode")
        if args.gain is not None:
            check(c, c.set_agc(0), "could not switch AGC to manual")
            check(c, c.set_gain(args.gain), "could not set gain")
        check(c, c.subscribe(SUBSCRIBE_FM), "could not subscribe to FM data")

        if not args.json:
            print(f"Tuning {freq_mhz:.1f} MHz, waiting for RDS...",
                  file=sys.stderr)

        start = time.monotonic()
        while True:
            if not c.connected:
                break
            if args.duration and (time.monotonic() - start) >= args.duration:
                break
            for mt, pl in c.recv_raw(timeout=0.2):
                if mt != MsgType.DATA_FM:
                    continue
                try:
                    final = parse_fm(pl).rds
                except FrameDecodeError as exc:
                    decode_errors += 1
                    print(f"skipping bad FM frame: {exc}", file=sys.stderr)
                    continue
                if final is None or args.json:
                    continue
                line = _rds_line(final, freq_mhz)
                if line != last_line:
                    print(line, flush=True)
                    last_line = line

        if args.json and final is not None:
            print_json(_rds_json(final, freq_mhz))
            json_printed = True
        if decode_errors and not args.json:
            print(f"{decode_errors} frame(s) skipped (bad decode)",
                  file=sys.stderr)

    except KeyboardInterrupt:
        if not args.json:
            print("\nStopped", file=sys.stderr)
    finally:
        if args.json and not json_printed:
            print_json(_rds_json(final, freq_mhz) if final is not None
                       else {"synced": False, "freq_mhz": freq_mhz})
        restore_radio(c, state)
        c.disconnect()


def _load_audio(path: str, target_rate: int) -> array.array:
    """Decode an audio file to mono float32 samples at target_rate.

    Uses ffmpeg for most formats, falls back to raw f32 LE for .pcm/.f32 files.
    """
    from ..fm_audio import AudioLoadError, load_audio
    try:
        return load_audio(path, target_rate)
    except AudioLoadError as exc:
        fail(str(exc))
