"""Frequency formatting and parsing utilities."""


_SUFFIX_MULTIPLIERS = {
    'k': 1_000,
    'khz': 1_000,
    'm': 1_000_000,
    'mhz': 1_000_000,
    'g': 1_000_000_000,
    'ghz': 1_000_000_000,
}


def format_freq(hz: int) -> str:
    """Return human-readable frequency with SI prefix and unit.

    < 1000         -> "900 Hz"
    1k – 1M        -> "9.6 kHz"
    1M – 1G        -> "100.1 MHz"
    >= 1G          -> "2.457 GHz"

    Trailing zeros stripped; no decimal for integer-scaled values.
    """
    hz = int(hz)
    if hz < 1_000:
        return f"{hz} Hz"

    if hz < 1_000_000:
        v = hz / 1_000
        unit = "kHz"
        decimals = 1
    elif hz < 1_000_000_000:
        v = hz / 1_000_000
        unit = "MHz"
        decimals = 1
    else:
        v = hz / 1_000_000_000
        unit = "GHz"
        decimals = 3

    s = f"{v:.{decimals}f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return f"{s} {unit}"


def _split_value_suffix(s: str) -> tuple[float, str]:
    """Return the numeric part and unit suffix of a frequency string."""
    s = s.strip()
    if not s:
        raise ValueError("empty frequency string")

    lower = s.lower()
    pos = len(lower)
    while pos > 0 and lower[pos - 1].isalpha():
        pos -= 1
    num_str = lower[:pos].strip()
    suffix = lower[pos:].strip()

    try:
        value = float(num_str)
    except ValueError:
        raise ValueError(f"invalid number '{num_str}'")
    return value, suffix


def parse_freq(s: str) -> int:
    """Parse a frequency string with optional SI suffix into Hz integer.

    Accepted: bare number (Hz), or number + suffix.
    Suffixes (case-insensitive): k/khz, m/mhz, g/ghz.
    Decimal numbers allowed.
    """
    value, suffix = _split_value_suffix(s)
    if suffix == '':
        return int(value)

    multiplier = _SUFFIX_MULTIPLIERS.get(suffix)
    if multiplier is None:
        raise ValueError(f"unknown suffix '{suffix}'")

    return int(round(value * multiplier))


def parse_tune_freq(s: str) -> int:
    """Parse a tuning entry, treating a bare value below 100 kHz as MHz.

    Radio users type ``100.1`` for 100.1 MHz, but a bare integer can also
    be a raw Hz value.  Suffixed entries and values of 100 kHz or more keep
    their literal meaning.
    """
    value, suffix = _split_value_suffix(s)
    if suffix:
        multiplier = _SUFFIX_MULTIPLIERS.get(suffix)
        if multiplier is None:
            raise ValueError(f"unknown suffix '{suffix}'")
        return int(round(value * multiplier))
    if value < 100_000:
        return int(round(value * 1_000_000))
    return int(value)
