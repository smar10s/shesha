from __future__ import annotations

import argparse
import json

from shesha.protocol import ACCUM_CODES, WINDOW_CODES, DaemonMode

from .common import (
    _connect, check, fail, print_json, restore_radio, snapshot_radio,
)


def do_fft(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        if not c.set_fft_size(args.size):
            fail("daemon rejected the FFT size")
        if args.json:
            print_json({"fft_size": args.size})
        else:
            print(f"FFT size: {args.size}")
    finally:
        c.disconnect()


def do_fps(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        if not c.set_fps(args.fps):
            fail("daemon rejected the frame rate")
        if args.json:
            print_json({"fps": args.fps})
        else:
            print(f"waterfall: {args.fps} fps")
    finally:
        c.disconnect()


def do_window(args: argparse.Namespace) -> None:
    code = WINDOW_CODES[args.window]
    c = _connect(args)
    try:
        check(c, c.set_window(code), f"could not set {args.window} window")
        if args.json:
            print_json({"window": args.window, "window_type": int(code)})
        else:
            print(f"window: {args.window}")
    finally:
        c.disconnect()


def do_accum(args: argparse.Namespace) -> None:
    code = ACCUM_CODES[args.accum]
    c = _connect(args)
    try:
        check(c, c.set_accum(code), f"could not set {args.accum} accumulation")
        if args.json:
            print_json({"accum": args.accum, "accum_mode": int(code)})
        else:
            print(f"accumulation: {args.accum}")
    finally:
        c.disconnect()


def do_workers(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        check(c, c.set_workers(args.workers),
              f"could not set {args.workers} PSD workers")
        if args.json:
            print_json({"workers": args.workers})
        else:
            print(f"PSD workers: {args.workers}")
    finally:
        c.disconnect()


def _psd_summary(result, status) -> dict:
    psd = result.psd
    if psd.frames == 0:
        fail("no PSD frames received")
    if psd.bin_count == 0:
        fail("no PSD bins received")

    bin_width_hz = status.sample_rate_hz / psd.bin_count
    peak_offset_hz = (psd.peak_bin - psd.bin_count // 2) * bin_width_hz
    return {
        "duration_s": result.duration,
        "frames": psd.frames,
        "fps": round(result.psd_fps, 3),
        "fft_size": psd.bin_count,
        "freq_mhz": psd.last_center_mhz,
        "coverage": status.psd_coverage,
        "workers": status.workers,
        "mean_db": round(psd.mean_db, 2),
        "peak_db": round(psd.peak_db, 2),
        "peak_freq_mhz": round(
            psd.peak_center_mhz + peak_offset_hz / 1e6, 6),
    }


def _write_psd_frame(fh, frame) -> None:
    json.dump({
        "ts": frame.timestamp,
        "center_mhz": frame.center_freq_mhz,
        "floor_db": frame.db_floor,
        "ceil_db": frame.db_ceil,
        "bins": frame.bins,
    }, fh)
    fh.write("\n")


def do_capture(args: argparse.Namespace) -> None:
    """Capture PSD frames and report a release-facing summary."""
    c = _connect(args)
    state = snapshot_radio(c)
    fh = None
    if args.out:
        try:
            fh = open(args.out, "w")
        except OSError as exc:
            fail(f"cannot write {args.out}: {exc}")
    try:
        check(c, c.set_mode(DaemonMode.WATERFALL),
              "could not switch to waterfall mode")
        sink = (lambda frame: _write_psd_frame(fh, frame)) if fh else None
        try:
            result = c.stream(duration=args.duration, psd_sink=sink)
        except OSError as exc:
            fail(f"cannot write {args.out}: {exc}")
        current = c.status()
        if current is None:
            fail("daemon not responding")
        status, _mode = current
        summary = _psd_summary(result, status)
        if args.out:
            summary["out"] = args.out
        if args.json:
            print_json(summary)
        else:
            print(f"PSD capture: {summary['frames']} frames in "
                  f"{summary['duration_s']:.1f} s "
                  f"({summary['fps']:.1f} fps)")
            print(f"  fft:  {summary['fft_size']} bins at "
                  f"{summary['freq_mhz']:.3f} MHz")
            print(f"  peak: {summary['peak_db']:.1f} dB at "
                  f"{summary['peak_freq_mhz']:.3f} MHz")
            print(f"  mean: {summary['mean_db']:.1f} dB, "
                  f"coverage {summary['coverage'] * 100:.0f}%")
            if args.out:
                print(f"wrote {args.out}")
    finally:
        if fh is not None:
            fh.close()
        restore_radio(c, state)
        c.disconnect()
