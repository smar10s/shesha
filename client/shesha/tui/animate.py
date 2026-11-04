"""Time-based animation transforms for shesha TUI elements.

Value-based: every animation drives a float (usually 0.0–1.0).
Callers interpret the value — color interpolation, position, opacity.
The animator knows nothing about terminal rendering; it's a pure
math engine usable by status bars, modes, and any future code.
"""
from __future__ import annotations
import math
import time
from typing import Callable


def _identity(t: float) -> float:
    return t


def _ease_in_quad(t: float) -> float:
    return t * t


def _ease_out_quad(t: float) -> float:
    return 1.0 - (1.0 - t) * (1.0 - t)


def _ease_in_out_quad(t: float) -> float:
    if t < 0.5:
        return 2.0 * t * t
    return 1.0 - (-2.0 * t + 2.0) ** 2 / 2.0


EASINGS: dict[str, Callable[[float], float]] = {
    'linear':       _identity,
    'ease_in':      _ease_in_quad,
    'ease_out':     _ease_out_quad,
    'ease_in_out':  _ease_in_out_quad,
}


# ─── Internal animation objects ───────────────────────────────────────────

class _ToAnim:
    __slots__ = ('target', 'duration', 'easing', 'elapsed', 'start_value',
                 'pending')

    def __init__(self, target: float, duration: float, easing: str,
                 start_value: float) -> None:
        self.target = target
        self.duration = max(duration, 0.001)
        self.easing = EASINGS.get(easing, _identity)
        self.elapsed = 0.0
        self.start_value = start_value
        self.pending: tuple[float, float, str] | None = None

    def tick(self, dt: float) -> None:
        self.elapsed += dt

    @property
    def value(self) -> float:
        t = min(1.0, self.elapsed / self.duration)
        return self.start_value + (self.target - self.start_value) * self.easing(t)

    @property
    def done(self) -> bool:
        return self.elapsed >= self.duration


class _PulseAnim:
    __slots__ = ('frequency', 'min_val', 'max_val', 'phase',
                 'settle_at', '_settle_transition_duration')

    def __init__(self, frequency: float, min_val: float, max_val: float,
                 phase_offset: float = 0.0) -> None:
        self.frequency = frequency
        self.min_val = min_val
        self.max_val = max_val
        self.phase = phase_offset
        self.settle_at: float | None = None
        self._settle_transition_duration = 0.3

    def tick(self, dt: float) -> None:
        self.phase += dt

    @property
    def value(self) -> float:
        t = math.sin(2.0 * math.pi * self.phase * self.frequency)
        return self.min_val + (t + 1.0) * 0.5 * (self.max_val - self.min_val)

    @property
    def settle_done(self) -> bool:
        return self.settle_at is not None and self.phase >= self.settle_at


class _FlashAnim:
    __slots__ = ('count', 'duration', 'period', 'elapsed')

    def __init__(self, count: int, duration: float) -> None:
        self.count = count
        self.duration = max(duration, 0.001)
        self.period = self.duration / count if count > 0 else 0.1
        self.elapsed = 0.0

    def tick(self, dt: float) -> None:
        self.elapsed += dt

    @property
    def value(self) -> float:
        if self.elapsed >= self.duration:
            return 0.0
        phase = (self.elapsed % self.period) / self.period
        return 1.0 if phase < 0.5 else 0.0

    @property
    def done(self) -> bool:
        return self.elapsed >= self.duration


# ─── Animator ─────────────────────────────────────────────────────────────

class Animator:
    """Value-based animation engine driven by wall-clock delta time.

    Owns a set of named animation slots. Call `tick(dt)` every frame,
    then read animation values with `get(name)`.

    Usage:
        a = Animator()
        a.pulse('status_text', frequency=2.5, min_val=0.3, max_val=1.0)
        a.to('fade', target=1.0, duration=0.5, easing='ease_out')
        a.flash('alert', count=3, duration=0.6)

        # Each frame:
        a.tick(dt)
        v = a.get('status_text')       # 0.3–1.0 oscillating
        if a.get('alert') > 0:   # currently flashing
            ...

        a.stop('status_text')          # remove animation, value → 0
    """

    def __init__(self) -> None:
        self._active: dict[str, _ToAnim | _PulseAnim | _FlashAnim] = {}
        self._last_values: dict[str, float] = {}

    def to(self, name: str, target: float = 1.0, duration: float = 0.5,
           easing: str = 'ease_out') -> None:
        """One-shot transition from current value to *target*.

        If an animation with this name is already running, picks up
        from its current value for a smooth continuation.
        """
        start = self._current_value(name)
        self._active[name] = _ToAnim(target, duration, easing, start)

    def to_queued(self, name: str, target: float = 1.0,
                  duration: float = 0.5, easing: str = 'ease_out') -> None:
        """Transition to *target* without interrupting one in flight.

        A running transition always finishes first.  If *target* changes
        while it runs, the newest request waits and starts on completion.
        """
        anim = self._active.get(name)
        if isinstance(anim, _ToAnim):
            anim.pending = (target, duration, easing)
            return
        self.to(name, target, duration, easing)

    def pulse(self, name: str, frequency: float = 2.0,
              min_val: float = 0.0, max_val: float = 1.0,
              phase_offset: float = 0.0) -> None:
        """Continuous sinusoidal oscillation. Runs until stopped.

        *phase_offset* advances the starting phase so the pulse begins
        at a particular point in the wave cycle.  At 1 Hz, 0.25 starts
        at the peak (value = max_val).
        """
        self._active[name] = _PulseAnim(frequency, min_val, max_val,
                                        phase_offset)

    def settle_pulse(self, name: str, delay: float = 1.0,
                     transition_duration: float = 0.3) -> None:
        """Let a running pulse complete *delay* more seconds of oscillation,
        then auto-transition to target=1.0 via ease_out.

        If the animation is not an active ``_PulseAnim``, this is a no-op.
        If settle is already scheduled, it is not rescheduled.
        """
        anim = self._active.get(name)
        if isinstance(anim, _PulseAnim) and anim.settle_at is None:
            anim.settle_at = anim.phase + delay
            anim._settle_transition_duration = transition_duration

    def flash(self, name: str, count: int = 3, duration: float = 0.6) -> None:
        """Burst of on/off flashes. Self-removes when done."""
        self._active[name] = _FlashAnim(count, duration)

    def stop(self, name: str) -> None:
        """Remove an animation. Value returns to 0."""
        self._active.pop(name, None)
        self._last_values.pop(name, None)

    def get(self, name: str) -> float:
        """Current value of animation *name*, or 0.0 if none. Does NOT advance time."""
        anim = self._active.get(name)
        if anim is None:
            return self._last_values.get(name, 0.0)
        return anim.value

    def running(self, name: str) -> bool:
        """True if an animation with *name* is active."""
        return name in self._active

    def tick(self, dt: float) -> None:
        """Advance all active animations by *dt* seconds.

        Call once per render frame. Removes completed to/flash animations.
        Replaces settled pulses with a smooth to(1.0) transition.
        """
        if dt <= 0:
            return
        done: list[str] = []
        for name, anim in list(self._active.items()):
            anim.tick(dt)
            self._last_values[name] = anim.value
            if isinstance(anim, _PulseAnim) and anim.settle_done:
                self._active[name] = _ToAnim(1.0, anim._settle_transition_duration,
                                             'ease_out', anim.value)
            elif isinstance(anim, _ToAnim) and anim.done:
                if anim.pending is not None:
                    target, duration, easing = anim.pending
                    anim.pending = None
                    if target != anim.target:
                        self._active[name] = _ToAnim(target, duration,
                                                     easing, anim.target)
                        continue
                done.append(name)
            elif isinstance(anim, _FlashAnim) and anim.done:
                done.append(name)
        for name in done:
            self._active.pop(name, None)

    def _current_value(self, name: str) -> float:
        """Snapshot of current value for a slot (0.0 if idle)."""
        anim = self._active.get(name)
        if anim is not None:
            return anim.value
        return self._last_values.get(name, 0.0)
