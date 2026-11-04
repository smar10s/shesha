"""Spectrum band annotation: frequency range database and label selection.

Maps the RF spectrum (70 MHz – 6 GHz) to human-recognizable service names
at any zoom level.  Works like a map: zoomed out you see "WiFi" at 5 GHz;
zoomed in you see individual channels.  Labels are selected to fit the
visible window width without overlapping.

Data sources are cited per-entry.  Frequencies are for ITU Region 2
(Americas), which covers both US (FCC) and Canada (ISED).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BandEntry:
    """One annotated frequency range or point.

    Point frequencies (like GPS L1) use lo_hz == hi_hz.
    Priority is 0–100: higher means more likely to survive label
    competition at a given zoom level.
    """
    lo_hz: int
    hi_hz: int
    label: str
    group: str       # grouping key: 'wifi', 'fm', 'noaa', 'lte', etc.
    priority: int    # 0–100, higher = more interesting


def select_labels(entries: list[BandEntry],
                  view_lo: int, view_hi: int,
                  width: int,
                  min_frac: float = 0.02,
                  max_frac: float = 0.85) -> list[BandEntry]:
    """Pick entries that fit the visible window at the current zoom.

    Filters by overlap with [view_lo, view_hi], then by visible width
    relative to the window (the map rule: too narrow is noise, too wide
    is invisible context).  Sorts by visible width descending, then
    priority descending.  Finally, drops entries whose rendered label
    would overlap a previously placed one.

    Returns the surviving entries in placement order (left to right).
    """
    view_span = view_hi - view_lo
    if view_span <= 0 or width < 1:
        return []

    candidates = []
    for e in entries:
        # Must overlap the window.
        vis_lo = max(e.lo_hz, view_lo)
        vis_hi = min(e.hi_hz, view_hi)
        if vis_lo > vis_hi:
            # Point frequencies: overlap means the point is in the window.
            if e.lo_hz == e.hi_hz and view_lo <= e.lo_hz <= view_hi:
                candidates.append((e, 0))
                continue
            continue

        vis_width = vis_hi - vis_lo
        frac = vis_width / view_span

        # Filter by zoom-level visibility.
        if frac < min_frac and e.lo_hz != e.hi_hz:
            continue
        # Skip entries that dwarf the window — unless fully contained,
        # which means the window is just slightly wider than the entry
        # (e.g. FM broadcast band inside the FM dial snap range).
        fully_contained = e.lo_hz >= view_lo and e.hi_hz <= view_hi
        if frac > max_frac and not fully_contained:
            continue

        candidates.append((e, vis_width))

    # Sort: widest first, then highest priority.
    candidates.sort(key=lambda t: (t[1], t[0].priority), reverse=True)

    # Place labels, rejecting overlaps.
    placed: list[tuple[int, int, BandEntry]] = []  # (col_start, col_end, entry)

    for e, _ in candidates:
        mid_hz = (max(e.lo_hz, view_lo) + min(e.hi_hz, view_hi)) / 2
        col = int((mid_hz - view_lo) / view_span * (width - 1))
        col = max(0, min(width - 1, col))

        label_len = len(e.label)
        start = col - label_len // 2
        start = max(0, min(width - label_len, start))
        end = start + label_len

        # Check overlap with already-placed labels (1-col gap).
        overlap = False
        for ps, pe, _ in placed:
            if start < pe + 1 and end > ps - 1:
                overlap = True
                break

        if not overlap:
            placed.append((start, end, e))

    # Return in left-to-right order.
    result_with_pos = [(s, e) for s, _, e in placed]
    result_with_pos.sort(key=lambda t: t[0])
    return [e for _, e in result_with_pos]


def render_band_labels(entries: list[BandEntry],
                       view_lo: int, view_hi: int,
                       width: int) -> str:
    """Render a single line of band annotation labels.

    Returns a string of exactly *width* characters with labels placed
    at the midpoint of each entry's visible range.
    """
    selected = select_labels(entries, view_lo, view_hi, width)
    if not selected:
        return ' ' * width

    view_span = view_hi - view_lo
    line = [' '] * width

    for e in selected:
        mid_hz = (max(e.lo_hz, view_lo) + min(e.hi_hz, view_hi)) / 2
        col = int((mid_hz - view_lo) / view_span * (width - 1))
        col = max(0, min(width - 1, col))

        text = e.label
        start = col - len(text) // 2
        start = max(0, min(width - len(text), start))

        for i, ch in enumerate(text):
            pos = start + i
            if 0 <= pos < width:
                line[pos] = ch

    return ''.join(line)


# ═══════════════════════════════════════════════════════════════════════════
# Frequency data — ITU Region 2 (Americas)
#
# Each entry cites its source.  Frequencies that differ between US/Canada
# are noted; most allocations are identical for FCC and ISED.
# ═══════════════════════════════════════════════════════════════════════════

BANDS: list[BandEntry] = [
    # ── Broadcast ────────────────────────────────────────────────────
    # Source: Wikipedia "FM broadcasting", Broadcast bands — 88–108 MHz (Americas)
    BandEntry(88_000_000, 108_000_000, 'FM Broadcast', 'broadcast', 80),

    # ── Cellular (LTE/5G, North America) ─────────────────────────────
    # Source: Wikipedia "LTE frequency bands", Frequency bands table
    BandEntry(617_000_000,  652_000_000, 'LTE B71 DL', 'cellular', 60),
    BandEntry(663_000_000,  698_000_000, 'LTE B71 UL', 'cellular', 60),
    BandEntry(699_000_000,  716_000_000, 'LTE B12 UL', 'cellular', 60),
    BandEntry(729_000_000,  746_000_000, 'LTE B12 DL', 'cellular', 60),
    BandEntry(746_000_000,  756_000_000, 'LTE B13 DL', 'cellular', 60),
    BandEntry(777_000_000,  787_000_000, 'LTE B13 UL', 'cellular', 60),
    BandEntry(824_000_000,  849_000_000, 'LTE B5 UL',  'cellular', 60),
    BandEntry(869_000_000,  894_000_000, 'LTE B5 DL',  'cellular', 60),
    BandEntry(1_710_000_000, 1_755_000_000, 'LTE B4 UL',  'cellular', 60),
    BandEntry(1_850_000_000, 1_910_000_000, 'LTE B2 UL',  'cellular', 60),
    BandEntry(1_930_000_000, 1_990_000_000, 'LTE B2 DL',  'cellular', 60),
    BandEntry(2_110_000_000, 2_155_000_000, 'LTE B4 DL',  'cellular', 60),
    BandEntry(2_500_000_000, 2_570_000_000, 'LTE B7 UL',  'cellular', 60),
    BandEntry(2_620_000_000, 2_690_000_000, 'LTE B7 DL',  'cellular', 60),
    # Source: Wikipedia "5G NR frequency bands", FR1 table
    BandEntry(2_496_000_000, 2_690_000_000, '5G n41',     'cellular', 60),
    BandEntry(3_300_000_000, 4_200_000_000, '5G n77',     'cellular', 60),
    BandEntry(3_300_000_000, 3_800_000_000, '5G n78',     'cellular', 60),

    # ── Aviation ─────────────────────────────────────────────────────
    # Source: Wikipedia "Airband", Spectrum usage
    BandEntry(108_000_000, 117_950_000, 'Air NAV', 'aviation', 55),
    BandEntry(118_000_000, 136_975_000, 'Air Voice', 'aviation', 55),
    BandEntry(121_500_000, 121_500_000, 'Air Emergency', 'aviation', 55),
    BandEntry(243_000_000, 243_000_000, 'Mil Guard', 'aviation', 50),
    BandEntry(329_300_000, 335_000_000, 'ILS GS', 'aviation', 50),
    BandEntry(960_000_000, 1_215_000_000, 'DME/TACAN', 'aviation', 50),
    # Source: Wikipedia "Automatic Dependent Surveillance–Broadcast"
    BandEntry(978_000_000,  978_000_000,  'ADS-B UAT',  'aviation', 65),
    BandEntry(1_090_000_000, 1_090_000_000, 'ADS-B 1090', 'aviation', 65),

    # ── Amateur ──────────────────────────────────────────────────────
    # Source: Wikipedia "Amateur radio frequency allocations", ITU Region 2
    BandEntry(144_000_000,   148_000_000,   '2m',       'amateur', 55),
    BandEntry(146_520_000,   146_520_000,   '2m FM Call','amateur', 55),
    BandEntry(222_000_000,   225_000_000,   '1.25m',    'amateur', 55),
    BandEntry(420_000_000,   450_000_000,   '70cm',     'amateur', 55),
    BandEntry(446_000_000,   446_000_000,   '70cm FM Call','amateur', 55),
    BandEntry(902_000_000,   928_000_000,   '33cm',     'amateur', 55),
    BandEntry(1_240_000_000, 1_300_000_000, '23cm',     'amateur', 55),
    BandEntry(2_300_000_000, 2_310_000_000, '13cm',     'amateur', 50),
    BandEntry(2_390_000_000, 2_450_000_000, '13cm',     'amateur', 50),
    BandEntry(3_300_000_000, 3_500_000_000, '9cm',      'amateur', 50),
    BandEntry(5_650_000_000, 5_925_000_000, '5cm',      'amateur', 50),

    # ── Marine ───────────────────────────────────────────────────────
    # Source: Wikipedia "Marine VHF radio"
    BandEntry(156_000_000, 174_000_000, 'Marine VHF',  'marine', 60),
    BandEntry(156_525_000, 156_525_000, 'Marine Ch 70','marine', 55),
    BandEntry(156_800_000, 156_800_000, 'Marine Ch 16','marine', 60),
    BandEntry(161_975_000, 161_975_000, 'AIS 1',       'marine', 55),
    BandEntry(162_025_000, 162_025_000, 'AIS 2',       'marine', 55),

    # ── Weather ──────────────────────────────────────────────────────
    # Source: Wikipedia "NOAA Weather Radio", Radio frequencies used
    BandEntry(162_400_000, 162_550_000, 'NOAA WX',   'weather', 75),
    BandEntry(162_400_000, 162_400_000, 'NOAA WX 2', 'weather', 70),
    BandEntry(162_425_000, 162_425_000, 'NOAA WX 4', 'weather', 70),
    BandEntry(162_450_000, 162_450_000, 'NOAA WX 5', 'weather', 70),
    BandEntry(162_475_000, 162_475_000, 'NOAA WX 3', 'weather', 70),
    BandEntry(162_500_000, 162_500_000, 'NOAA WX 6', 'weather', 70),
    BandEntry(162_525_000, 162_525_000, 'NOAA WX 7', 'weather', 70),
    BandEntry(162_550_000, 162_550_000, 'NOAA WX 1', 'weather', 70),

    # ── ISM ──────────────────────────────────────────────────────────
    # Source: Wikipedia "ISM radio band", Frequency allocations table
    BandEntry(902_000_000,   928_000_000,   'ISM 915',  'ism', 50),
    BandEntry(2_400_000_000, 2_500_000_000, 'ISM 2.4G', 'ism', 50),
    BandEntry(5_725_000_000, 5_875_000_000, 'ISM 5.8G', 'ism', 50),

    # ── Personal radio ───────────────────────────────────────────────
    # Source: Wikipedia "Multi-Use Radio Service", Frequencies
    BandEntry(151_820_000, 154_600_000, 'MURS', 'personal', 30),
    # Source: Wikipedia "Family Radio Service", List of FRS channels
    BandEntry(462_550_000, 467_712_500, 'FRS/GMRS', 'personal', 35),

    # ── IoT ──────────────────────────────────────────────────────────
    # Source: Wikipedia "LoRa", Description — US915 (902–928 MHz)
    BandEntry(902_000_000, 928_000_000, 'LoRa 915', 'iot', 45),

    # ── Satellite ────────────────────────────────────────────────────
    # Source: Wikipedia "GPS signals", Legacy/Modernization sections
    BandEntry(1_176_450_000, 1_176_450_000, 'GPS L5', 'satellite', 70),
    BandEntry(1_227_600_000, 1_227_600_000, 'GPS L2', 'satellite', 70),
    BandEntry(1_575_420_000, 1_575_420_000, 'GPS L1', 'satellite', 70),
    # Source: Wikipedia "Iridium satellite constellation", Air interface
    BandEntry(1_616_000_000, 1_626_500_000, 'Iridium', 'satellite', 45),
    # Source: Wikipedia "Globalstar", System architecture
    BandEntry(1_610_000_000, 1_618_725_000, 'Globalstar UL', 'satellite', 40),
    BandEntry(2_483_500_000, 2_500_000_000, 'Globalstar DL', 'satellite', 40),

    # ── WiFi (overall bands) ─────────────────────────────────────────
    # Source: Wikipedia "List of WLAN channels"
    BandEntry(2_400_000_000, 2_500_000_000, 'WiFi 2.4G', 'wifi', 70),
    BandEntry(5_150_000_000, 5_835_000_000, 'WiFi 5G',   'wifi', 70),

    # ── WiFi 2.4 GHz channels (center ± 11 MHz, North America 1–11) ─
    # Source: Wikipedia "List of WLAN channels", §2.4 GHz
    BandEntry(2_401_000_000, 2_423_000_000, 'WiFi Ch 1',  'wifi', 40),
    BandEntry(2_406_000_000, 2_428_000_000, 'WiFi Ch 2',  'wifi', 40),
    BandEntry(2_411_000_000, 2_433_000_000, 'WiFi Ch 3',  'wifi', 40),
    BandEntry(2_416_000_000, 2_438_000_000, 'WiFi Ch 4',  'wifi', 40),
    BandEntry(2_421_000_000, 2_443_000_000, 'WiFi Ch 5',  'wifi', 40),
    BandEntry(2_426_000_000, 2_448_000_000, 'WiFi Ch 6',  'wifi', 40),
    BandEntry(2_431_000_000, 2_453_000_000, 'WiFi Ch 7',  'wifi', 40),
    BandEntry(2_436_000_000, 2_458_000_000, 'WiFi Ch 8',  'wifi', 40),
    BandEntry(2_441_000_000, 2_463_000_000, 'WiFi Ch 9',  'wifi', 40),
    BandEntry(2_446_000_000, 2_468_000_000, 'WiFi Ch 10', 'wifi', 40),
    BandEntry(2_451_000_000, 2_473_000_000, 'WiFi Ch 11', 'wifi', 40),

    # ── WiFi 5 GHz channels (center ± 10 MHz, 20 MHz BW) ────────────
    # Source: Wikipedia "List of WLAN channels", §5 GHz
    # UNII-1
    BandEntry(5_170_000_000, 5_190_000_000, 'WiFi Ch 36',  'wifi', 40),
    BandEntry(5_190_000_000, 5_210_000_000, 'WiFi Ch 40',  'wifi', 40),
    BandEntry(5_210_000_000, 5_230_000_000, 'WiFi Ch 44',  'wifi', 40),
    BandEntry(5_230_000_000, 5_250_000_000, 'WiFi Ch 48',  'wifi', 40),
    # UNII-2
    BandEntry(5_250_000_000, 5_270_000_000, 'WiFi Ch 52',  'wifi', 40),
    BandEntry(5_270_000_000, 5_290_000_000, 'WiFi Ch 56',  'wifi', 40),
    BandEntry(5_290_000_000, 5_310_000_000, 'WiFi Ch 60',  'wifi', 40),
    BandEntry(5_310_000_000, 5_330_000_000, 'WiFi Ch 64',  'wifi', 40),
    # UNII-2 Extended / DFS
    BandEntry(5_490_000_000, 5_510_000_000, 'WiFi Ch 100', 'wifi', 40),
    BandEntry(5_510_000_000, 5_530_000_000, 'WiFi Ch 104', 'wifi', 40),
    BandEntry(5_530_000_000, 5_550_000_000, 'WiFi Ch 108', 'wifi', 40),
    BandEntry(5_550_000_000, 5_570_000_000, 'WiFi Ch 112', 'wifi', 40),
    BandEntry(5_570_000_000, 5_590_000_000, 'WiFi Ch 116', 'wifi', 40),
    BandEntry(5_590_000_000, 5_610_000_000, 'WiFi Ch 120', 'wifi', 40),
    BandEntry(5_610_000_000, 5_630_000_000, 'WiFi Ch 124', 'wifi', 40),
    BandEntry(5_630_000_000, 5_650_000_000, 'WiFi Ch 128', 'wifi', 40),
    BandEntry(5_650_000_000, 5_670_000_000, 'WiFi Ch 132', 'wifi', 40),
    BandEntry(5_670_000_000, 5_690_000_000, 'WiFi Ch 136', 'wifi', 40),
    BandEntry(5_690_000_000, 5_710_000_000, 'WiFi Ch 140', 'wifi', 40),
    BandEntry(5_710_000_000, 5_730_000_000, 'WiFi Ch 144', 'wifi', 40),
    # UNII-3
    BandEntry(5_735_000_000, 5_755_000_000, 'WiFi Ch 149', 'wifi', 40),
    BandEntry(5_755_000_000, 5_775_000_000, 'WiFi Ch 153', 'wifi', 40),
    BandEntry(5_775_000_000, 5_795_000_000, 'WiFi Ch 157', 'wifi', 40),
    BandEntry(5_795_000_000, 5_815_000_000, 'WiFi Ch 161', 'wifi', 40),
    BandEntry(5_815_000_000, 5_835_000_000, 'WiFi Ch 165', 'wifi', 40),
]
