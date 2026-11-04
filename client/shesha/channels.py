"""WiFi channel <-> frequency mappings (MHz)."""

CHANNELS_24GHZ: dict[int, int] = {
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432,
    6: 2437, 7: 2442, 8: 2447, 9: 2452, 10: 2457,
    11: 2462, 12: 2467, 13: 2472, 14: 2484,
}

CHANNELS_5GHZ: dict[int, int] = {
    32: 5160, 36: 5180, 40: 5200, 44: 5220, 48: 5240,
    52: 5260, 56: 5280, 60: 5300, 64: 5320, 68: 5340,
    72: 5360, 76: 5380, 80: 5400, 84: 5420, 88: 5440,
    92: 5460, 96: 5480, 100: 5500, 104: 5520, 108: 5540,
    112: 5560, 116: 5580, 120: 5600, 124: 5620, 128: 5640,
    132: 5660, 136: 5680, 140: 5700, 144: 5720, 149: 5745,
    153: 5765, 157: 5785, 161: 5805, 165: 5825, 169: 5845,
    173: 5865, 177: 5885,
}

CHANNEL_TO_FREQ: dict[int, int] = CHANNELS_24GHZ | CHANNELS_5GHZ
FREQ_TO_CHANNEL: dict[int, int] = {f: ch for ch, f in CHANNEL_TO_FREQ.items()}


def freq_to_channel(freq_mhz: float) -> int:
    """Convert frequency (MHz) to WiFi channel number.

    Returns 0 if freq is not near any known WiFi channel (>10 MHz away).
    """
    freq_int = int(freq_mhz)
    if freq_int in FREQ_TO_CHANNEL:
        return FREQ_TO_CHANNEL[freq_int]
    nearest = min(FREQ_TO_CHANNEL, key=lambda f: abs(f - freq_int))
    if abs(nearest - freq_int) > 10:
        return 0
    return FREQ_TO_CHANNEL[nearest]
