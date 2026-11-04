from __future__ import annotations

import argparse
import json
import sys
import time

from shesha.client import PushResult, SubscriptionError
from shesha.protocol import DaemonMode, FrameDecodeError

from .common import (
    AGC_NAMES, RadioState, _connect, check, fail, print_json, resolve_freq,
    restore_radio,
)

IQ_DRAIN_TIMEOUT = 30.0


def do_iq_bits(args: argparse.Namespace) -> None:
    """Set the IQ wire sample width (8 or 12), both directions."""
    c = _connect(args)
    try:
        check(c, c.set_iq_bits(args.bits), "could not set IQ sample width")
        if args.json:
            print_json({"iq_bits": args.bits})
        else:
            print(f"IQ sample width: {args.bits} bits")
    finally:
        c.disconnect()


def build_iq_meta(status, *, out, bits, n_samples, n_bytes, duration_s,
                  dropped_frames, timestamp) -> dict:
    """Compose the self-describing sidecar for an IQ capture.

    Records the settings a capture cannot carry itself, so a later analysis
    never has to guess the gain/AGC/BW it was taken at.
    """
    return {
        "out": out,
        "timestamp": timestamp,
        "freq_hz": status.freq_hz,
        "sample_rate_hz": status.sample_rate_hz,
        "bw_hz": status.bw_hz,
        "gain_db": status.gain_db,
        "agc_mode": status.agc_mode,
        "iq_bits": bits,
        "n_samples": n_samples,
        "n_bytes": n_bytes,
        "duration_s": duration_s,
        "dropped_frames": dropped_frames,
    }


def write_iq_meta(out_path: str, meta: dict) -> str:
    """Write `<out_path>.meta.json` and return its path."""
    path = out_path + ".meta.json"
    with open(path, "w") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
    return path


def read_iq_meta(path: str) -> dict:
    """Read the sidecar next to an IQ file, if one exists."""
    meta_path = path + ".meta.json"
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        fail(f"cannot read sidecar {meta_path}: {exc}")
    if not isinstance(meta, dict):
        fail(f"invalid sidecar {meta_path}: expected an object")
    return meta


def replay_settings(args: argparse.Namespace, meta: dict
                    ) -> tuple[int, int | None, int | None]:
    """Resolve bits, sample rate, and frequency from flags or the sidecar."""
    bits = args.bits if args.bits is not None else meta.get("iq_bits", 12)
    if bits not in (8, 12):
        fail(f"sidecar has invalid iq_bits: {bits}")

    freq_mhz = resolve_freq(args)
    if freq_mhz is not None:
        freq_hz = int(freq_mhz * 1e6)
    else:
        try:
            freq_hz = (int(meta["freq_hz"])
                       if meta.get("freq_hz") is not None else None)
        except (TypeError, ValueError):
            fail("sidecar has invalid freq_hz")

    if args.sr is not None:
        sr_hz = int(args.sr * 1e6)
    else:
        try:
            sr_hz = (int(meta["sample_rate_hz"])
                     if meta.get("sample_rate_hz") is not None else None)
        except (TypeError, ValueError):
            fail("sidecar has invalid sample_rate_hz")
    if sr_hz is not None and sr_hz <= 0:
        fail("sidecar has invalid sample_rate_hz")
    return bits, sr_hz, freq_hz


def do_iq_rx(args: argparse.Namespace) -> None:
    """Capture raw IQ to a file.

    Enters IQ mode, sets sr/bw/bits, and streams DATA_IQ frames into a
    raw packed file (12-bit: 3 B/sample, 8-bit: 2 B/sample — the exact
    wire format, see firmware/src/iq_pack.h).  Prints a summary on
    exit.  Ctrl-C stops early but keeps the data.
    """
    c = _connect(args)
    state = None

    n_samples = 0
    n_bytes = 0
    first_ts = None
    last_ts = None
    rate = 0

    try:
        st, prev_mode = c.status() or (None, None)
        if st:
            state = RadioState.from_status(st, prev_mode)

        check(c, c.set_mode(DaemonMode.IQ), "could not switch to IQ mode")
        if args.sr is not None:
            check(c, c.set_sample_rate(int(args.sr * 1e6)),
                  "could not set sample rate")
        if args.bw is not None:
            check(c, c.set_bw(int(args.bw * 1e6)),
                  "could not set bandwidth")
        check(c, c.set_iq_bits(args.bits), "could not set IQ sample width")
        freq_mhz = resolve_freq(args)
        if freq_mhz is not None:
            check(c, c.set_freq(int(freq_mhz * 1e6)),
                  "could not set frequency")
        if args.gain is not None:
            check(c, c.set_agc(0), "could not switch AGC to manual")
            check(c, c.set_gain(args.gain), "could not set gain")

        st_now = c.status()
        status_now = st_now[0] if st_now else None

        try:
            fh = open(args.out, 'wb')
        except OSError as exc:
            fail(f"cannot write {args.out}: {exc}")

        try:
            with fh:
                for frame in c.iq_rx(duration=args.duration or 0.0):
                    fh.write(frame.samples)
                    n_samples += frame.n_samples
                    n_bytes += len(frame.samples)
                    rate = frame.sample_rate_hz
                    if first_ts is None:
                        first_ts = frame.timestamp
                    last_ts = frame.timestamp
        except SubscriptionError as exc:
            fail(str(exc))
        except FrameDecodeError as exc:
            fail(f"bad IQ frame: {exc}")
        except KeyboardInterrupt:
            pass

        out = {
            "n_samples": n_samples,
            "n_bytes": n_bytes,
            "bits": args.bits,
            "sample_rate_hz": rate,
            "duration_s": round(last_ts - first_ts, 3)
                          if first_ts is not None and last_ts is not None
                          else None,
            "dropped_frames": c.dropped,
        }
        if status_now is not None:
            out.update({
                "freq_hz": status_now.freq_hz,
                "bw_hz": status_now.bw_hz,
                "gain_db": status_now.gain_db,
                "agc": AGC_NAMES.get(status_now.agc_mode, "?"),
            })
        if args.meta and status_now is not None:
            meta = build_iq_meta(
                status_now, out=args.out, bits=args.bits,
                n_samples=n_samples, n_bytes=n_bytes,
                duration_s=out["duration_s"], dropped_frames=c.dropped,
                timestamp=time.time())
            try:
                out["meta"] = write_iq_meta(args.out, meta)
            except OSError as exc:
                fail(f"cannot write metadata for {args.out}: {exc}")
        if args.json:
            print_json(out)
        else:
            duration_text = ("unknown duration"
                             if out["duration_s"] is None
                             else f"{out['duration_s']} s")
            print(f"captured {duration_text}, {n_samples} samples "
                  f"({n_bytes} bytes)")
            print(f"wrote {args.out}")
            if "meta" in out:
                print(f"metadata: {out['meta']}")
    finally:
        restore_radio(c, state)
        c.disconnect()


def do_iq_tx(args: argparse.Namespace) -> None:
    """Stream a raw packed IQ file to the DAC.

    Chunks the file in 4096-sample pieces, honoring RESP_RETRY by waiting
    and re-sending the same chunk (the daemon ring is bounded; a client
    ahead of realtime is normal).  END on EOF lets the daemon drain; the
    command waits for that drain before disconnecting.
    """
    needs_meta = (args.bits is None or args.sr is None
                  or (getattr(args, "freq", None) is None
                      and getattr(args, "channel", None) is None))
    meta = read_iq_meta(args.file) if needs_meta else {}
    bits, sr_hz, freq_hz = replay_settings(args, meta)

    state = None
    started = False
    drained = False
    c = _connect(args)

    sent = 0
    retries = 0
    status_now = None

    try:
        st, prev_mode = c.status() or (None, None)
        if st:
            state = RadioState.from_status(st, prev_mode)

        check(c, c.set_mode(DaemonMode.IQ), "could not switch to IQ mode")
        check(c, c.set_iq_bits(bits), "could not set IQ sample width")
        if sr_hz is not None:
            check(c, c.set_sample_rate(sr_hz), "could not set sample rate")
        if freq_hz is not None:
            check(c, c.set_freq(freq_hz), "could not set frequency")
        if args.tx_atten is not None:
            check(c, c.set_tx_atten(args.tx_atten),
                  "could not set TX attenuation")

        st_now = c.status()
        status_now = st_now[0] if st_now else None

        try:
            fh = open(args.file, 'rb')
        except OSError as exc:
            fail(f"cannot read {args.file}: {exc}")

        bps = 3 if bits == 12 else 2
        chunk = 4096 * bps

        with fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                started = True
                while True:
                    r = c.iq_tx_stream_data(data)
                    if r == PushResult.OK:
                        sent += len(data)
                        break
                    if r == PushResult.RETRY:
                        retries += 1
                        time.sleep(0.01)
                        continue
                    detail = c.last_error
                    fail("IQ push failed"
                         + (f": {detail}" if detail else ""))

        check(c, c.iq_tx_stream_end(), "could not end IQ stream")

        deadline = time.monotonic() + IQ_DRAIN_TIMEOUT
        while time.monotonic() < deadline:
            ts = c.tx_stats()
            if ts is not None and not ts.iq_active:
                drained = True
                break
            time.sleep(0.05)
        if not drained:
            fail("IQ stream did not drain")

        if args.json:
            out = {"sent_bytes": sent, "retries": retries, "bits": bits}
            if status_now is not None:
                out.update({
                    "freq_hz": status_now.freq_hz,
                    "sample_rate_hz": status_now.sample_rate_hz,
                    "tx_atten_db": status_now.tx_atten_db,
                })
            print_json(out)
        else:
            print(f"streamed {sent} bytes ({bits}-bit IQ), "
                  f"{retries} retries")
    except KeyboardInterrupt:
        print("interrupted — TX stopped", file=sys.stderr)
    finally:
        if started and not drained:
            c.tx_stop()
        restore_radio(c, state)
        c.disconnect()
