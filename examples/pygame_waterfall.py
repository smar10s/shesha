#!/usr/bin/env python3
"""Draw a live waterfall from DATA_PSD frames using pygame.

This is the same daemon protocol as the TUI and CLI. It exists to show that
a Shesha client does not have to be a terminal application.

    python3 examples/pygame_waterfall.py --freq 90.1

Headless (no window, saves a PNG and prints peak bins):

    SDL_VIDEODRIVER=dummy python3 examples/pygame_waterfall.py \
        --headless --frames 20 --out waterfall.png --stats
"""

import argparse
import os
import time

# Importing pygame prints a banner unless this is set before the import.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

from shesha.client import DaemonClient
from shesha.protocol import (DaemonMode, MsgType, SUBSCRIBE_PSD,
                             parse_psd)


def _palette():
    colours = []
    for i in range(256):
        v = i / 255.0
        red = int(255 * max(0.0, min(1.0, 3.0 * v - 1.5)))
        green = int(255 * max(0.0, min(1.0, 3.0 * v - 0.5)))
        blue = int(255 * max(0.0, min(1.0, 2.0 * v)))
        colours.append((red, green, blue))
    return colours


PALETTE = _palette()


def auto_range(bins, low=0.15, high=0.995):
    """Robust display range: drop nulls and the hottest stray bin."""
    ordered = sorted(bins)
    if not ordered:
        return -100.0, 0.0
    floor = ordered[int((len(ordered) - 1) * low)]
    ceil = ordered[int((len(ordered) - 1) * high)]
    if ceil - floor < 10.0:
        ceil = floor + 10.0
    return floor, ceil


def _level(db, floor, ceil):
    if ceil <= floor:
        return 0
    v = (db - floor) / (ceil - floor)
    return max(0, min(255, int(v * 255)))


def colorize(bins, floor, ceil):
    """One-pixel-high surface, one column per bin."""
    row = pygame.Surface((len(bins), 1))
    for x, db in enumerate(bins):
        row.set_at((x, 0), PALETTE[_level(db, floor, ceil)])
    return row


class Waterfall:
    """A scrolling history of PSD rows, newest at the top."""

    def __init__(self, history=240):
        self.history = history
        self.surface = None

    def push(self, bins, floor, ceil):
        row = colorize(bins, floor, ceil)
        if self.surface is None or self.surface.get_width() != row.get_width():
            self.surface = pygame.Surface((row.get_width(), self.history))
            self.surface.fill((0, 0, 0))
        self.surface.scroll(0, 1)
        self.surface.blit(row, (0, 0))

    def render(self, size):
        return pygame.transform.scale(self.surface, size)


def render_rows(rows, floor, ceil, size):
    """Build a waterfall from oldest-first rows. Used by the offline test."""
    if not rows:
        raise ValueError("no rows")
    width = len(rows[0])
    height = max(1, min(len(rows), size[1]))
    surface = pygame.Surface((width, height))
    for y, bins in enumerate(reversed(rows[-height:])):
        surface.blit(colorize(bins, floor, ceil), (0, y))
    return pygame.transform.scale(surface, size)


def _configure(client, args):
    client.set_mode(DaemonMode.WATERFALL)
    client.set_freq(round(args.freq * 1e6))
    client.set_bw(round(args.bw * 1e6))
    client.set_fft_size(args.fft)
    client.set_fps(args.fps)
    if args.gain is not None:
        client.set_agc(0)
        client.set_gain(args.gain)
    client.subscribe(SUBSCRIBE_PSD)
    status = client.status()
    return status[0] if status else None


def _peak(frame, status):
    bins = frame.bins
    peak = max(range(len(bins)), key=bins.__getitem__)
    if not status.sample_rate_hz:
        return frame.center_freq_mhz, bins[peak]
    offset = (peak - len(bins) // 2) * status.sample_rate_hz / len(bins)
    return frame.center_freq_mhz + offset / 1e6, bins[peak]


def _capture(client, args, status):
    rows = []
    deadline = time.monotonic() + args.timeout
    while len(rows) < args.frames and time.monotonic() < deadline:
        for msg_type, payload in client.recv_raw(timeout=0.25):
            if msg_type != MsgType.DATA_PSD:
                continue
            frame = parse_psd(payload)
            rows.append(frame.bins)
            if args.stats:
                mhz, db = _peak(frame, status)
                print(f"{frame.timestamp:.3f}s  floor {frame.db_floor:6.1f}  "
                      f"peak {db:6.1f} dBFS at {mhz:.3f} MHz")
            if len(rows) >= args.frames:
                break
    return rows


def _run_window(client, args):
    pygame.init()
    screen = pygame.display.set_mode((args.width, args.height))
    pygame.display.set_caption("shesha waterfall")
    clock = pygame.time.Clock()
    waterfall = Waterfall()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        for msg_type, payload in client.recv_raw(timeout=0.01):
            if msg_type == MsgType.DATA_PSD:
                frame = parse_psd(payload)
                floor, ceil = auto_range(frame.bins)
                if args.floor is not None:
                    floor = args.floor
                if args.ceil is not None:
                    ceil = args.ceil
                waterfall.push(frame.bins, floor, ceil)
        screen.fill((0, 0, 0))
        if waterfall.surface is not None:
            screen.blit(waterfall.render((args.width, args.height)), (0, 0))
        pygame.display.flip()
        clock.tick(60)
    pygame.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.2.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--freq", type=float, default=90.1, help="MHz")
    parser.add_argument("--bw", type=float, default=20.0, help="MHz")
    parser.add_argument("--gain", type=float, default=None,
                        help="manual gain in dB; leaves AGC alone if omitted")
    parser.add_argument("--fft", type=int, default=1024)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--floor", type=float, default=None,
                        help="dBFS floor; auto-scale if omitted")
    parser.add_argument("--ceil", type=float, default=None,
                        help="dBFS ceiling; auto-scale if omitted")
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--out", default="waterfall.png")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        pygame.init()

    client = DaemonClient(args.host, args.port)
    client.connect()
    try:
        status = _configure(client, args)
        if status is None:
            print("daemon did not report status")
            return 1
        if args.headless:
            rows = _capture(client, args, status)
            if not rows:
                print("no PSD frames received")
                return 1
            floor, ceil = auto_range([v for row in rows for v in row])
            if args.floor is not None:
                floor = args.floor
            if args.ceil is not None:
                ceil = args.ceil
            image = render_rows(rows, floor, ceil, (args.width, args.height))
            pygame.image.save(image, args.out)
            print(f"saved {len(rows)} frames to {args.out}")
        else:
            _run_window(client, args)
    finally:
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
