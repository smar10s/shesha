from __future__ import annotations

import argparse

from .common import AGC_CODES, _connect, check, print_json


def _emit(args, data: dict, text: str) -> None:
    if args.json:
        print_json(data)
    else:
        print(text)


def do_freq(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        hz = int(round(args.mhz * 1e6))
        check(c, c.set_freq(hz), "could not set frequency")
        _emit(args, {"freq_mhz": args.mhz, "freq_hz": hz},
              f"frequency: {args.mhz:.3f} MHz")
    finally:
        c.disconnect()


def do_bw(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        hz = int(round(args.mhz * 1e6))
        check(c, c.set_bw(hz), "could not set bandwidth")
        _emit(args, {"bw_mhz": args.mhz, "bw_hz": hz},
              f"bandwidth: {args.mhz:.3f} MHz")
    finally:
        c.disconnect()


def do_gain(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        # Radio gain only applies in manual AGC.  The daemon stores it and
        # returns OK in the other modes, so switch first rather than silently
        # reporting a gain that the hardware never took.
        check(c, c.set_agc(0), "could not switch AGC to manual")
        check(c, c.set_gain(args.db), "could not set gain")
        _emit(args, {"gain_db": args.db, "agc": "manual", "agc_mode": 0},
              f"gain: {args.db:.1f} dB (manual)")
    finally:
        c.disconnect()


def do_agc(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        code = AGC_CODES[args.mode]
        check(c, c.set_agc(code), f"could not set {args.mode} AGC")
        _emit(args, {"agc": args.mode, "agc_mode": code},
              f"AGC: {args.mode}")
    finally:
        c.disconnect()


def do_sample_rate(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        hz = int(round(args.msps * 1e6))
        check(c, c.set_sample_rate(hz), "could not set sample rate")
        _emit(args,
              {"sample_rate_msps": args.msps, "sample_rate_hz": hz},
              f"sample rate: {args.msps:.3f} MSPS")
    finally:
        c.disconnect()
