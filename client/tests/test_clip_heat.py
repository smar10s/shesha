"""Turning lifetime ADC rail counters into a clip latch."""
from shesha.tui.clip_heat import (CLIP_THRESHOLD, HOLD_S, TRANSITION_S,
                                  ClipMeter, HeatDriver)


def _driver():
    from shesha.tui.animate import Animator
    return HeatDriver(Animator())


def test_meter_first_sample_only_establishes_a_baseline():
    m = ClipMeter()
    assert m.update(1000, 50_000) == 0.0


def test_meter_differences_successive_counters():
    m = ClipMeter()
    m.update(1000, 50_000)
    assert abs(m.update(1055, 51_000) - 0.055) < 1e-9


def test_meter_treats_a_counter_reset_as_zero():
    m = ClipMeter()
    m.update(10_000, 500_000)
    assert m.update(5, 1000) == 0.0
    # The reset becomes the new baseline, so the next deltas are real.
    assert abs(m.update(15, 2000) - 0.01) < 1e-9
    assert abs(m.update(65, 3000) - 0.05) < 1e-9


def test_meter_ignores_a_status_with_no_new_samples():
    m = ClipMeter()
    m.update(100, 10_000)
    assert m.update(200, 10_000) == 0.0


def test_latch_trips_above_the_threshold():
    d = _driver()
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)  # 10% rail rate
    assert d.clipping


def test_latch_ignores_rates_at_or_below_the_threshold():
    d = _driver()
    d.update(0, 0, now=0.0)
    d.update(int(CLIP_THRESHOLD * 10_000), 10_000, now=0.3)
    assert not d.clipping


def test_latch_holds_through_a_quiet_interval():
    d = _driver()
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)  # trip
    d.update(1000, 20_000, now=2.0)  # quiet, still inside the hold
    assert d.clipping


def test_latch_releases_after_the_hold():
    d = _driver()
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)  # trip, hold until 2.8
    d.update(1000, 20_000, now=HOLD_S + 0.31)
    assert not d.clipping


def test_latch_retriggers_on_a_later_packet():
    d = _driver()
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)
    d.update(2000, 20_000, now=2.3)  # another clipped interval extends the hold
    d.update(2000, 30_000, now=HOLD_S + 1.0)  # quiet, but hold was extended
    assert d.clipping


def test_driver_sets_and_clears_the_animator_slot():
    from shesha.tui.animate import Animator

    a = Animator()
    d = HeatDriver(a)
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)
    a.tick(1.0)
    assert a.get('gain_heat') == 1.0

    d.update(1000, 20_000, now=HOLD_S + 0.31)
    a.tick(1.0)
    assert a.get('gain_heat') == 0.0


def test_driver_completes_a_fade_before_reversing():
    from shesha.tui.animate import Animator

    a = Animator()
    d = HeatDriver(a, hold_s=0.05)
    d.update(0, 0, now=0.0)
    d.update(1000, 10_000, now=0.3)   # trip
    d.update(1000, 20_000, now=0.45)  # release mid-fade

    a.tick(TRANSITION_S * 0.5)
    assert 0.0 < a.get('gain_heat') < 1.0

    a.tick(TRANSITION_S * 0.5)
    assert a.get('gain_heat') == 1.0, "warn fade must complete first"

    a.tick(TRANSITION_S)
    assert a.get('gain_heat') == 0.0, "then fade back to fg"
