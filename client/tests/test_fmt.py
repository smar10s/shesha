"""Tests for frequency formatting and parsing."""
from shesha.tui.fmt import format_freq, parse_freq, parse_tune_freq


def test_format_freq_hz():
    assert format_freq(0) == "0 Hz"
    assert format_freq(900) == "900 Hz"


def test_format_freq_khz():
    assert format_freq(9_600) == "9.6 kHz"
    assert format_freq(1_000) == "1 kHz"
    assert format_freq(500_500) == "500.5 kHz"


def test_format_freq_mhz():
    assert format_freq(100_100_000) == "100.1 MHz"
    assert format_freq(20_000_000) == "20 MHz"
    assert format_freq(1_000_000) == "1 MHz"
    assert format_freq(500_500_000) == "500.5 MHz"


def test_format_freq_ghz():
    assert format_freq(2_457_000_000) == "2.457 GHz"
    assert format_freq(1_000_000_000) == "1 GHz"
    assert format_freq(5_180_000_000) == "5.18 GHz"


def test_parse_freq_bare_hz():
    assert parse_freq("5180000000") == 5180000000
    assert parse_freq("20") == 20
    assert parse_freq("9600") == 9600


def test_parse_freq_suffix_upper():
    assert parse_freq("100.1M") == 100100000
    assert parse_freq("100.1MHZ") == 100100000
    assert parse_freq("2.8G") == 2800000000
    assert parse_freq("2.8GHZ") == 2800000000
    assert parse_freq("9.6K") == 9600
    assert parse_freq("9.6KHZ") == 9600


def test_parse_freq_suffix_lower():
    assert parse_freq("100.1m") == 100100000
    assert parse_freq("2.8g") == 2800000000
    assert parse_freq("9.6k") == 9600
    assert parse_freq("100.1mhz") == 100100000


def test_parse_freq_decimal():
    assert parse_freq("0.5M") == 500000
    assert parse_freq("1.0G") == 1000000000


def test_parse_freq_invalid():
    import pytest
    with pytest.raises(ValueError):
        parse_freq("")
    with pytest.raises(ValueError):
        parse_freq("abc")
    with pytest.raises(ValueError):
        parse_freq("100X")


def test_parse_tune_freq_bare_number_is_mhz():
    assert parse_tune_freq("100.1") == 100_100_000
    assert parse_tune_freq("88") == 88_000_000
    assert parse_tune_freq("2457") == 2_457_000_000


def test_parse_tune_freq_keeps_suffix_and_raw_hz():
    assert parse_tune_freq("100.1M") == 100_100_000
    assert parse_tune_freq("9600k") == 9_600_000
    assert parse_tune_freq("100100000") == 100_100_000
    assert parse_tune_freq("5180000000") == 5_180_000_000
