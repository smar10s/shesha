from __future__ import annotations

import argparse

from shesha.client import MODE_NAMES, _caps_to_dict, _status_to_dict
from shesha.protocol import Capabilities, Status, WINDOW_NAMES

from .common import AGC_NAMES, TX_PROTO_NAMES, _connect, fail, print_json


def format_status(status: Status, mode: int) -> str:
    tx_state = "busy" if status.tx_busy else "idle"
    if status.tx_busy:
        tx_state += f" ({TX_PROTO_NAMES.get(status.tx_proto, '?')})"
    workers = f"{status.workers} worker" + (
        "" if status.workers == 1 else "s")
    if not status.fpga_active:
        fpga_state = "off"
    elif status.fpga_running:
        fpga_state = "on (running)"
    else:
        fpga_state = "on (not running)"
    return "\n".join([
        f"mode:       {MODE_NAMES.get(mode, 'unknown')}",
        f"frequency:  {status.freq_hz / 1e6:.3f} MHz",
        f"bandwidth:  {status.bw_hz / 1e6:.3f} MHz",
        f"sample:     {status.sample_rate_hz / 1e6:.3f} MSPS",
        f"gain:       {status.gain_db:.1f} dB "
        f"({AGC_NAMES.get(status.agc_mode, '?')})",
        f"fft:        {status.fft_size} bins "
        f"({WINDOW_NAMES.get(status.window_type, '?')}), "
        f"{status.target_fps} fps",
        f"psd:        {workers}, "
        f"{status.psd_coverage * 100:.1f}% coverage",
        f"iq:         {status.iq_bits}-bit",
        f"tx:         {tx_state}, atten {status.tx_atten_db:.1f} dB",
        f"rx:         {'active' if status.rx_active else 'idle'}",
        f"fpga:       {fpga_state}",
    ])


def format_caps(caps: Capabilities) -> str:
    features = []
    if caps.features & 1:
        features.append("waterfall")
    if caps.features & 2:
        features.append("wifi decode")
    if caps.features & 4:
        features.append("fabric wifi")
    if caps.features & 8:
        features.append("iq")
    return "\n".join([
        f"firmware build: {caps.fw_build_id}",
        f"fpga build:     {caps.fpga_build_id}",
        f"features:       {', '.join(features) or 'none'}",
        f"frequency:      {caps.min_freq_mhz} - {caps.max_freq_mhz} MHz",
        f"max sample:     {caps.max_sample_rate / 1e6:.3f} MHz",
        f"max fft:        {caps.max_fft_size}",
    ])


def do_status(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        result = c.status()
        if result is None:
            fail("no status response from daemon"
                 + (f": {c.last_error}" if c.last_error else ""))
        status, mode = result
        if args.json:
            print_json(_status_to_dict(status, mode))
        else:
            print(format_status(status, mode))
    finally:
        c.disconnect()


def do_caps(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        caps = c.caps()
        if caps is None:
            fail("no capabilities response from daemon"
                 + (f": {c.last_error}" if c.last_error else ""))
        if args.json:
            print_json(_caps_to_dict(caps))
        else:
            print(format_caps(caps))
    finally:
        c.disconnect()
