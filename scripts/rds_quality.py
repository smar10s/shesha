#!/usr/bin/env python3
"""Reproducible RDS signal-quality measurement.

This promotes the throwaway /tmp tools used to characterise 90.1 into one
committed, stdlib-only script, so an "analog path limitation" can be
measured and recorded instead of remembered.

Three subcommands:

    unpack  12-bit packed iq rx output -> interleaved float32
    analyze one IQ capture: band-limit + decode via build/rds_offline
    monitor live RDS yield in fixed windows, for watching a fade or an A/B

Signal quality is the decode itself: groups received, block errors and sync
losses from the exact firmware chain, plus capture rms.  A scalar CNR
estimate from the wideband capture was tried and deliberately dropped:
neither the +-120 kHz vs 300-900 kHz noise ratio nor a constant-envelope
amplitude-variance proxy predicts whether RDS decodes, because an adjacent
channel inflates both, and the on-air signal is not stationary between
captures.  Report the group yield, not a dB.

Examples:
    scripts/rds_quality.py unpack cap.f32q cap.f32
    scripts/rds_quality.py analyze --bandlimit 200 cap.f32
    scripts/rds_quality.py monitor --freq 90.1 --gain 35 --duration 120
"""
import argparse
import array
import json
import os
import subprocess
import sys
import time

IQ_RATE = 2_100_000.0
DEFAULT_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "build", "rds_offline")


def unpack12(src: str, dst: str) -> int:
    """Unpack 12-bit packed IQ (3 bytes/sample, iq_pack.h layout) to f32.

    Layout: byte0 = I[7:0]; byte1 low nibble = I[11:8], high nibble =
    Q[3:0]; byte2 = Q[11:4].  Values are sign-extended then scaled by
    1/2048 so full scale is ~1.0, matching the AD9361's 12-bit range.
    """
    data = open(src, "rb").read()
    n = len(data) // 3
    out = array.array("f", bytes(8 * n))
    for i in range(n):
        b0 = data[3 * i]
        b1 = data[3 * i + 1]
        b2 = data[3 * i + 2]
        ii = b0 | ((b1 & 0x0F) << 8)
        qq = (b1 >> 4) | (b2 << 4)
        if ii & 0x800:
            ii -= 0x1000
        if qq & 0x800:
            qq -= 0x1000
        out[2 * i] = ii / 2048.0
        out[2 * i + 1] = qq / 2048.0
    with open(dst, "wb") as fh:
        out.tofile(fh)
    return n


def run_offline(path: str, bandlimit_khz: float = 0.0,
                tool: str = DEFAULT_TOOL) -> dict:
    tool = os.path.normpath(tool)
    cmd = [tool]
    if bandlimit_khz:
        cmd += ["--bandlimit", str(bandlimit_khz)]
    cmd += ["--json", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{tool} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def cmd_unpack(args):
    n = unpack12(args.src, args.dst)
    print(json.dumps({"src": args.src, "dst": args.dst,
                      "samples": n}, indent=2))


def cmd_analyze(args):
    result = run_offline(args.file, args.bandlimit, args.tool)
    print(json.dumps(result, indent=2))


def _win(prev, cur, key):
    if prev is None:
        return 0
    return getattr(cur, key) - getattr(prev, key)


def cmd_monitor(args):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "client"))
    from shesha.client import DaemonClient
    from shesha.protocol import DaemonMode, MsgType, parse_fm, SUBSCRIBE_FM

    c = DaemonClient(args.host, args.port)
    c.connect()
    c.set_freq(int(args.freq * 1e6))
    c.set_mode(DaemonMode.FM)
    if args.agc is not None:
        c.set_agc(args.agc)
    if args.gain is not None:
        c.set_agc(0)
        c.set_gain(args.gain)
    c.subscribe(SUBSCRIBE_FM)
    time.sleep(0.5)

    prev = None
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.duration:
            last = None
            t_end = time.monotonic() + args.window
            while time.monotonic() < t_end:
                for mt, pl in c.recv_raw(timeout=0.2):
                    if mt == MsgType.DATA_FM:
                        last = parse_fm(pl).rds
            if last is None:
                print(json.dumps({"t": round(time.monotonic() - t0, 1),
                                  "frames": 0}), flush=True)
                continue
            row = {
                "t": round(time.monotonic() - t0, 1),
                "synced": int(last.synced),
                "pi": last.pi_text,
                "groups": _win(prev, last, "groups"),
                "block_errors": _win(prev, last, "block_errors"),
                "sync_losses": _win(prev, last, "sync_losses"),
                "ps": last.ps,
                "rt_valid": int(last.rt_valid),
            }
            prev = last
            print(json.dumps(row), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        c.disconnect()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pu = sub.add_parser("unpack", help="12-bit packed IQ -> interleaved f32")
    pu.add_argument("src")
    pu.add_argument("dst")
    pu.set_defaults(func=cmd_unpack)

    pa = sub.add_parser("analyze", help="decode one capture")
    pa.add_argument("file")
    pa.add_argument("--bandlimit", type=float, default=0.0,
                    help="channel filter in kHz before decoding (use 200 for "
                         "raw IQ-mode captures; omit for live captures)")
    pa.add_argument("--tool", default=DEFAULT_TOOL)
    pa.set_defaults(func=cmd_analyze)

    pm = sub.add_parser("monitor", help="live RDS yield in fixed windows")
    pm.add_argument("--host", default="192.168.2.1")
    pm.add_argument("--port", type=int, default=5555)
    pm.add_argument("--freq", type=float, required=True, help="MHz")
    pm.add_argument("--duration", type=float, default=60.0)
    pm.add_argument("--window", type=float, default=5.0)
    pm.add_argument("--gain", type=float, default=None)
    pm.add_argument("--agc", type=int, default=None, choices=[0, 1, 2])
    pm.set_defaults(func=cmd_monitor)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
