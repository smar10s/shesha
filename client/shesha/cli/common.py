"""Shared helpers for the headless CLI."""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from typing import NoReturn

from ..channels import CHANNEL_TO_FREQ
from ..client import DaemonClient
from ..protocol import DaemonMode, TX_PROTO_NAMES


AGC_CODES = {"manual": 0, "fast": 1, "slow": 2}
AGC_NAMES = {code: name for name, code in AGC_CODES.items()}


@dataclass(frozen=True)
class RadioState:
    """Radio settings that a temporary mode switch can disturb."""

    mode: int
    bw_hz: int
    sample_rate_hz: int
    agc_mode: int
    gain_db: float
    fpga_active: bool

    @classmethod
    def from_status(cls, status, mode) -> "RadioState":
        return cls(
            mode=int(mode), bw_hz=status.bw_hz,
            sample_rate_hz=status.sample_rate_hz,
            agc_mode=status.agc_mode, gain_db=status.gain_db,
            fpga_active=status.fpga_active)


def finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {text}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a finite number")
    return value


def positive_float(text: str) -> float:
    value = finite_float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def non_negative_float(text: str) -> float:
    value = finite_float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def bounded_int(minimum: int, maximum: int, label: str):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid integer: {text}") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}")
        return value
    return parse


def add_json(parser: argparse.ArgumentParser, *,
             default: bool | str = False) -> None:
    parser.add_argument("--json", action="store_true", default=default,
                        help="emit machine-readable JSON")


def add_freq(parser: argparse.ArgumentParser, *,
             required: bool = False,
             help: str = "frequency in MHz") -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--freq", type=positive_float, help=help)
    group.add_argument("--channel", type=int,
                       help="WiFi channel (mapped to MHz)")


def resolve_freq(args: argparse.Namespace) -> float | None:
    channel = getattr(args, "channel", None)
    if channel is not None:
        freq = CHANNEL_TO_FREQ.get(channel)
        if freq is None:
            fail(f"unknown WiFi channel: {channel}")
        return float(freq)
    return getattr(args, "freq", None)


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def check(c: DaemonClient, ok: bool, action: str) -> None:
    if ok:
        return
    detail = getattr(c, "last_error", None)
    fail(f"{action}: {detail}" if detail else action)


def print_json(data) -> None:
    print(json.dumps(data, indent=2))


def _connect(args: argparse.Namespace) -> DaemonClient:
    c = DaemonClient(host=args.host, port=args.port)
    try:
        c.connect()
    except OSError as exc:
        fail(f"connect failed: {exc}")
    return c


def snapshot_radio(c: DaemonClient) -> RadioState | None:
    result = c.status()
    if result is None:
        return None
    st, mode = result
    return RadioState.from_status(st, mode)


def restore_radio(c: DaemonClient, state: RadioState | None) -> None:
    """Undo settings changed implicitly by leaving and returning to a mode.

    Explicit tuning while staying in one mode is left alone: if the mode and
    FPGA state are unchanged, this is a no-op.
    """
    if state is None:
        return
    # Cleanup runs in a finally.  A dead socket would otherwise raise here
    # and mask the error that ended the command.
    try:
        result = c.status()
        if result is None:
            return
        current, current_mode = result
        current_mode = int(current_mode)

        same_mode = current_mode == state.mode
        same_fpga = bool(current.fpga_active) == state.fpga_active
        if same_mode and same_fpga:
            return

        if not same_mode:
            if not c.set_mode(DaemonMode(state.mode)):
                return
        if state.fpga_active:
            c.set_fpga(True)
        elif current.fpga_active:
            c.set_fpga(False)

        c.set_bw(state.bw_hz)
        c.set_sample_rate(state.sample_rate_hz)
        c.set_agc(state.agc_mode)
        if state.agc_mode == 0 and not state.fpga_active:
            c.set_gain(state.gain_db)
    except ConnectionError:
        return
