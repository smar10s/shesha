"""ADC clipping detection for the status-bar gain field.

The firmware reports lifetime rail counts.  The meter differences them
between status samples and trips a latch when enough components hit the
rails.  The latch holds for a few seconds so a duty-cycled transmitter
does not flicker the field.  Clipping is a binary fact; a gradient from
clean to overdriven would only imply precision that is not there.
"""
from __future__ import annotations

import time

CLIP_THRESHOLD = 0.01
HOLD_S = 2.5
TRANSITION_S = 0.25


class ClipMeter:
    """Differences the daemon's lifetime counters into a clip rate."""

    def __init__(self) -> None:
        self._clips: int | None = None
        self._samples: int | None = None

    def update(self, clips: int, samples: int) -> float:
        prev_clips = self._clips
        prev_samples = self._samples
        self._clips = clips
        self._samples = samples

        if prev_clips is None or prev_samples is None:
            return 0.0
        clip_delta = clips - prev_clips
        sample_delta = samples - prev_samples
        if clip_delta < 0 or sample_delta <= 0:
            return 0.0
        return clip_delta / sample_delta


class HeatDriver:
    """Latches ADC clipping and drives the status-bar gain colour."""

    def __init__(self, animator, hold_s: float = HOLD_S) -> None:
        self._animator = animator
        self._meter = ClipMeter()
        self._hold_s = hold_s
        self._clipping = False
        self._hold_until = 0.0

    @property
    def clipping(self) -> bool:
        return self._clipping

    def update(self, clips: int, samples: int,
               now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()

        if self._meter.update(clips, samples) > CLIP_THRESHOLD:
            self._hold_until = now + self._hold_s

        clipping = now < self._hold_until
        if clipping == self._clipping:
            return
        self._clipping = clipping
        target = 1.0 if clipping else 0.0
        self._animator.to_queued('gain_heat', target, TRANSITION_S,
                                 'ease_out')
