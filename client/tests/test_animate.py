"""Tests for the animation engine."""
from shesha.tui.animate import Animator


def test_to_basic():
    a = Animator()
    a.to('x', target=1.0, duration=1.0, easing='linear')
    assert a.get('x') == 0.0
    a.tick(0.5)
    assert abs(a.get('x') - 0.5) < 0.01
    a.tick(0.5)
    assert abs(a.get('x') - 1.0) < 0.01
    a.tick(0.1)
    assert a.get('x') == 1.0


def test_pulse_oscillates():
    a = Animator()
    a.pulse('p', frequency=1.0, min_val=0.0, max_val=1.0)
    a.tick(0.25)
    assert a.get('p') > 0.9


def test_pulse_settle():
    a = Animator()
    a.pulse('p', frequency=1.0, min_val=0.0, max_val=1.0)
    a.tick(0.1)
    a.settle_pulse('p', delay=0.5, transition_duration=0.3)
    a.tick(0.5)
    a.tick(0.5)
    assert a.get('p') > 0.8


def test_flash():
    a = Animator()
    a.flash('f', count=2, duration=1.0)
    a.tick(0.1)
    assert a.get('f') == 1.0
    a.tick(0.2)
    assert a.get('f') == 0.0
    a.tick(1.0)
    assert a.get('f') == 0.0


def test_stop():
    a = Animator()
    a.pulse('x', frequency=1.0, min_val=0.0, max_val=1.0)
    a.tick(0.25)
    assert a.get('x') > 0
    a.stop('x')
    assert a.get('x') == 0.0
    assert not a.running('x')


def test_to_queued_finishes_the_transition_in_flight():
    a = Animator()
    a.to_queued('x', 1.0, duration=1.0, easing='linear')
    a.tick(0.5)
    assert abs(a.get('x') - 0.5) < 1e-9

    a.to_queued('x', 0.0, duration=1.0, easing='linear')
    a.tick(0.25)
    assert abs(a.get('x') - 0.75) < 1e-9, "second request must not interrupt"

    a.tick(0.25)
    assert abs(a.get('x') - 1.0) < 1e-9, "first transition completes"

    a.tick(0.5)
    assert abs(a.get('x') - 0.5) < 1e-9, "queued transition is running"


def test_to_queued_keeps_only_the_newest_request():
    a = Animator()
    a.to_queued('x', 1.0, duration=1.0, easing='linear')
    a.tick(0.5)
    a.to_queued('x', 0.0, duration=1.0, easing='linear')
    a.to_queued('x', 1.0, duration=1.0, easing='linear')

    a.tick(0.5)
    assert abs(a.get('x') - 1.0) < 1e-9
    a.tick(1.0)
    assert abs(a.get('x') - 1.0) < 1e-9, "superseded request must not run"
