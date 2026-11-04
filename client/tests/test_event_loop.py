"""EventLoop drains UI actions before each select."""
from shesha.tui.event_loop import EventLoop


class _Callbacks:
    def __init__(self):
        self.running = True
        self.actions = 0
        self.frames = 0

    def on_actions(self):
        self.actions += 1

    def check_resize(self):
        self.running = False

    def get_daemon_fd(self):
        return None

    def on_data(self):
        pass

    def on_check_disconnect(self):
        pass

    def on_heartbeat(self):
        pass

    def on_frame(self, dt):
        self.frames += 1


def test_event_loop_drains_actions_before_selecting():
    callbacks = _Callbacks()
    EventLoop(fps=60.0).run(callbacks)
    assert callbacks.actions == 1


def test_event_loop_uses_the_monotonic_clock(monkeypatch):
    """Wall-clock jumps (NTP, suspend) must not stall or burst the UI."""
    import shesha.tui.event_loop as el

    def wall_clock():
        raise AssertionError('event loop read the wall clock')

    monkeypatch.setattr(el.time, 'time', wall_clock)
    callbacks = _Callbacks()
    EventLoop(fps=60.0).run(callbacks)
    assert callbacks.actions == 1
