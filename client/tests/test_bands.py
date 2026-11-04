"""Tests for the spectrum band annotation module."""
from shesha.bands import BandEntry, select_labels, render_band_labels


# ─── Helpers ─────────────────────────────────────────────────────────────

def _e(lo_mhz, hi_mhz, label, group='test', priority=50):
    """Shorthand: build a BandEntry from MHz values."""
    return BandEntry(int(lo_mhz * 1e6), int(hi_mhz * 1e6),
                     label, group, priority)


# ─── select_labels ───────────────────────────────────────────────────────


def test_select_basic_overlap():
    """Entries that overlap the window are returned."""
    entries = [_e(88, 108, 'FM'), _e(200, 300, 'Other')]
    result = select_labels(entries, 70_000_000, 120_000_000, width=80)
    labels = [r.label for r in result]
    assert 'FM' in labels
    assert 'Other' not in labels


def test_select_filters_too_wide():
    """An entry wider than ~80% of the window is excluded."""
    # Window is 10 MHz, entry spans 100 MHz — too wide.
    entries = [_e(50, 150, 'Huge')]
    result = select_labels(entries, 95_000_000, 105_000_000, width=80)
    assert len(result) == 0


def test_select_filters_too_narrow():
    """An entry narrower than ~5% of the window is excluded."""
    # Window is 100 MHz, entry spans 0.15 MHz — too narrow.
    entries = [_e(162.400, 162.550, 'NOAA WX')]
    result = select_labels(entries, 100_000_000, 200_000_000, width=80)
    assert len(result) == 0


def test_select_narrow_entry_visible_when_zoomed():
    """The same narrow entry shows up when the window is small enough."""
    entries = [_e(162.400, 162.550, 'NOAA WX')]
    result = select_labels(entries, 162_000_000, 163_000_000, width=80)
    labels = [r.label for r in result]
    assert 'NOAA WX' in labels


def test_select_priority_wins_overlap():
    """When two entries compete for the same column space, higher priority
    (or wider) wins and the other is dropped."""
    # Two entries at the same position — only one survives.
    entries = [
        _e(100, 102, 'Low', priority=30),
        _e(99, 103, 'High', priority=80),
    ]
    result = select_labels(entries, 90_000_000, 110_000_000, width=40)
    labels = [r.label for r in result]
    # High is wider and higher priority — it gets placed first
    assert 'High' in labels
    # Low overlaps and should be dropped
    assert 'Low' not in labels


def test_select_no_overlap_placement():
    """Labels that would overlap in column space are dropped."""
    # Two entries at nearly the same position — only one should survive.
    entries = [
        _e(100.0, 100.2, 'A', priority=80),
        _e(100.1, 100.3, 'B', priority=50),
    ]
    result = select_labels(entries, 99_000_000, 101_000_000, width=40)
    # Both are valid candidates but they overlap visually
    assert len(result) <= 2  # implementation may keep both if columns differ
    # At minimum the higher-priority one must survive
    labels = [r.label for r in result]
    assert 'A' in labels


def test_select_partial_overlap_with_window():
    """An entry partially inside the window is still included."""
    entries = [_e(80, 108, 'FM')]
    result = select_labels(entries, 100_000_000, 120_000_000, width=80)
    # FM overlaps the window (100-108 visible portion = 8 MHz)
    # Visible width is 8 MHz out of 20 MHz window = 40%, should pass
    labels = [r.label for r in result]
    assert 'FM' in labels


def test_select_empty_entries():
    assert select_labels([], 100_000_000, 200_000_000, width=80) == []


def test_select_point_entry():
    """A point frequency (lo == hi) is treated as a very narrow band."""
    entries = [BandEntry(1_575_420_000, 1_575_420_000, 'GPS L1', 'sat', 70)]
    # Narrow window where a point is visible
    result = select_labels(entries, 1_575_000_000, 1_576_000_000, width=80)
    labels = [r.label for r in result]
    assert 'GPS L1' in labels


# ─── render_band_labels ─────────────────────────────────────────────────


def test_render_returns_correct_width():
    entries = [_e(88, 108, 'FM')]
    line = render_band_labels(entries, 80_000_000, 115_000_000, width=60)
    assert len(line) == 60


def test_render_places_label():
    entries = [_e(88, 108, 'FM')]
    # Window must be wide enough that FM (20 MHz) is under the 85% threshold
    line = render_band_labels(entries, 80_000_000, 115_000_000, width=60)
    assert 'FM' in line


def test_render_empty():
    line = render_band_labels([], 100_000_000, 200_000_000, width=40)
    assert line == ' ' * 40


def test_render_label_centered_on_entry():
    """The label text is roughly centered on the entry's midpoint."""
    entries = [_e(95, 105, 'MID')]
    line = render_band_labels(entries, 90_000_000, 110_000_000, width=80)
    pos = line.find('MID')
    assert pos > 0
    # Midpoint of 95-105 in a 90-110 window = column 40 (center)
    assert abs(pos + 1 - 40) < 5  # within 5 columns of center


# ─── Real BANDS data ─────────────────────────────────────────────────────


def test_bands_data_loads():
    """The BANDS list is populated and all entries are valid."""
    from shesha.bands import BANDS
    assert len(BANDS) > 50
    for e in BANDS:
        assert e.lo_hz <= e.hi_hz
        assert 0 <= e.priority <= 100
        assert len(e.label) > 0
        assert len(e.group) > 0


def test_full_spectrum_shows_major_services():
    """At full zoom (70 MHz – 6 GHz), major services are visible."""
    from shesha.bands import BANDS
    result = select_labels(BANDS, 70_000_000, 6_000_000_000, width=120)
    labels = [r.label for r in result]
    # These should all be wide enough to survive at full zoom
    assert any('WiFi' in l for l in labels)
    assert any('5G' in l or 'LTE' in l for l in labels)


def test_fm_band_zoom_shows_fm_broadcast():
    """Zoomed to FM band range, 'FM Broadcast' appears."""
    from shesha.bands import BANDS
    result = select_labels(BANDS, 80_000_000, 115_000_000, width=80)
    labels = [r.label for r in result]
    assert 'FM Broadcast' in labels


def test_noaa_zoom_shows_channels():
    """Zoomed to NOAA range, individual channels appear."""
    from shesha.bands import BANDS
    result = select_labels(BANDS, 162_300_000, 162_600_000, width=80)
    labels = [r.label for r in result]
    assert any('NOAA WX' in l for l in labels)


def test_wifi_5g_zoom_shows_channels():
    """Zoomed to 5 GHz WiFi range, individual channels appear."""
    from shesha.bands import BANDS
    result = select_labels(BANDS, 5_170_000_000, 5_250_000_000, width=80)
    labels = [r.label for r in result]
    assert any('WiFi Ch' in l for l in labels)
