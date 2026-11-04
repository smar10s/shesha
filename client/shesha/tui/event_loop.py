"""Selector-based event loop with frame timing.

Manages:
- Registering/unregistering daemon FD
- Select with timeout derived from target FPS
- Calling on_data() when daemon socket is readable
- Calling on_frame(dt) at RENDER_INTERVAL
- Calling on_heartbeat() at heartbeat interval
"""
from __future__ import annotations

import selectors
import time
from typing import Protocol


class EventCallbacks(Protocol):
    """Protocol that the Shell (or any host) implements."""

    def on_actions(self) -> None:
        """Run work queued by other threads. UI thread only."""
        ...

    def on_data(self) -> None:
        """Called when daemon socket is readable."""
        ...

    def on_frame(self, dt: float) -> None:
        """Called at render interval with elapsed time."""
        ...

    def on_heartbeat(self) -> None:
        """Called at heartbeat interval."""
        ...

    def on_check_disconnect(self) -> None:
        """Called to check for daemon disconnect."""
        ...

    def get_daemon_fd(self) -> int | None:
        """Return the daemon socket file descriptor, or None."""
        ...

    def check_resize(self) -> None:
        """Check for terminal resize."""
        ...

    @property
    def running(self) -> bool:
        """Whether the loop should continue."""
        ...


class EventLoop:
    """Selector-based event loop with frame timing.

    Drives the main loop: select on daemon FD, render at target FPS,
    heartbeat at a slower interval.
    """

    def __init__(self, fps: float = 60.0, heartbeat_interval: float = 0.3,
                 disconnect_timeout: float = 5.0) -> None:
        self.fps = fps
        self._render_interval = 1.0 / fps
        self._heartbeat_interval = heartbeat_interval
        self._disconnect_timeout = disconnect_timeout

    def run(self, callbacks: EventCallbacks) -> None:
        """Run until callbacks.running becomes False."""
        sel = selectors.DefaultSelector()
        current_fd: int | None = None
        last_render = time.monotonic()
        last_hb = 0.0

        try:
            while callbacks.running:
                callbacks.on_actions()
                callbacks.check_resize()

                fd = callbacks.get_daemon_fd()
                if fd != current_fd:
                    if current_fd is not None:
                        try:
                            sel.unregister(current_fd)
                        except (KeyError, ValueError):
                            pass
                    if fd is not None:
                        sel.register(fd, selectors.EVENT_READ, data='daemon')
                    current_fd = fd

                now = time.monotonic()
                timeout = max(0.0, last_render + self._render_interval - now)
                events = sel.select(timeout=timeout)

                for key, _mask in events:
                    if key.data == 'daemon':
                        callbacks.on_data()

                now = time.monotonic()

                # Heartbeat / disconnect check
                if now - last_hb >= self._heartbeat_interval:
                    last_hb = now
                    callbacks.on_check_disconnect()
                    callbacks.on_heartbeat()

                # Frame rendering
                if now >= last_render + self._render_interval:
                    dt = now - last_render
                    last_render = now
                    callbacks.on_frame(dt)
        finally:
            sel.close()
