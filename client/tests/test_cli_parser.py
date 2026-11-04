"""The release CLI has six top-level groups and no agent-only commands."""
import pytest

from shesha.cli.parser import build_parser
from shesha.protocol import MAX_FM_AUDIO_RATE_HZ


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_status_is_the_default_status_command():
    args = parse("status")
    assert args.func.__name__ == "do_status"
    assert args.json is False


def test_status_caps_is_nested_under_status():
    args = parse("status", "caps")
    assert args.func.__name__ == "do_caps"
    assert args.json is False

    args = parse("status", "caps", "--json")
    assert args.json is True


def test_status_json_before_caps_is_preserved():
    args = parse("status", "--json", "caps")
    assert args.func.__name__ == "do_caps"
    assert args.json is True


def test_radio_commands_parse_values():
    assert parse("radio", "freq", "100.1").mhz == 100.1
    assert parse("radio", "bw", "20").mhz == 20.0
    assert parse("radio", "gain", "40.5").db == 40.5
    assert parse("radio", "agc", "fast").mode == "fast"
    assert parse("radio", "sample-rate", "20").msps == 20.0


@pytest.mark.parametrize(
    "argv",
    [
        ("wifi", "scan", "--channel", "6"),
        ("fm", "rx", "--channel", "6"),
        ("fm", "tx", "--channel", "6", "--file", "track.mp3"),
        ("fm", "rds", "--channel", "6"),
        ("iq", "rx", "--out", "capture.bin", "--channel", "6"),
        ("iq", "tx", "--file", "capture.bin", "--channel", "6"),
    ],
)
def test_channel_is_an_alias_for_freq(argv):
    args = parse(*argv)
    assert args.channel == 6
    assert args.freq is None


def test_freq_and_channel_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse("wifi", "scan", "--freq", "2437", "--channel", "6")


def test_wifi_tx_beacon_accepts_freq():
    args = parse("wifi", "tx", "beacon", "--freq", "2437")
    assert args.freq == 2437.0
    assert args.channel is None


def test_viz_commands_parse_values():
    assert parse("viz", "fft", "4096").size == 4096
    assert parse("viz", "fps", "20").fps == 20


@pytest.mark.parametrize("size", ["64", "128"])
def test_viz_fft_rejects_sizes_below_firmware_minimum(size):
    with pytest.raises(SystemExit):
        parse("viz", "fft", size)


def test_viz_fft_accepts_firmware_minimum():
    assert parse("viz", "fft", "256").size == 256


def test_viz_settings_parse():
    assert parse("viz", "window", "hann").window == "hann"
    assert parse("viz", "accum", "avg").accum == "avg"
    assert parse("viz", "workers", "2").workers == 2


def test_viz_capture_parses():
    args = parse("viz", "capture", "--duration", "2", "--out", "psd.jsonl")
    assert args.func.__name__ == "do_capture"
    assert args.duration == 2.0
    assert args.out == "psd.jsonl"


def test_wifi_scan_parses():
    args = parse("wifi", "scan", "--freq", "2437", "--duration", "5",
                 "--agc", "fast")
    assert args.freq == 2437.0
    assert args.duration == 5.0
    assert args.agc == "fast"
    assert args.func.__name__ == "do_scan"


def test_wifi_tx_beacon_parses():
    args = parse("wifi", "tx", "beacon", "--ssid", "TEST", "--channel", "6")
    assert args.ssid == "TEST"
    assert args.channel == 6
    assert args.func.__name__ == "do_beacon"


def test_wifi_tx_frame_beacon_parses():
    args = parse(
        "wifi", "tx", "frame", "--type", "beacon", "--channel", "6",
        "--ssid", "TEST", "--bssid", "aa:bb:cc:dd:ee:ff",
        "--da", "ff:ff:ff:ff:ff:ff", "--sa", "11:22:33:44:55:66",
        "--seq", "5", "--capability", "0x0411",
        "--beacon-interval", "200", "--ie", "221:0102",
        "--ie", "3:01", "--rate", "12", "--count", "3",
        "--interval", "5000", "--cyclic", "--json")

    assert args.type == "beacon"
    assert args.func.__name__ == "do_frame"
    assert args.channel == 6
    assert args.ssid == "TEST"
    assert args.bssid == "aa:bb:cc:dd:ee:ff"
    assert args.da == "ff:ff:ff:ff:ff:ff"
    assert args.sa == "11:22:33:44:55:66"
    assert args.seq == 5
    assert args.capability == 0x0411
    assert args.beacon_interval == 200
    assert args.ie == ["221:0102", "3:01"]
    assert args.rate == 12
    assert args.count == 3
    assert args.interval == 5000
    assert args.cyclic is True
    assert args.json is True


def test_wifi_tx_frame_deauth_parses():
    args = parse(
        "wifi", "tx", "frame", "--type", "deauth", "--freq", "2437",
        "--bssid", "aa:bb:cc:dd:ee:ff", "--da", "11:22:33:44:55:66",
        "--reason", "3")

    assert args.type == "deauth"
    assert args.func.__name__ == "do_frame"
    assert args.freq == 2437.0
    assert args.reason == 3


def test_wifi_tx_frame_raw_parses_hex_or_file():
    args = parse("wifi", "tx", "frame", "--type", "raw",
                 "--psdu-hex", "80000000")
    assert args.psdu_hex == "80000000"
    assert args.psdu_file is None

    args = parse("wifi", "tx", "frame", "--type", "raw",
                 "--psdu-file", "frame.bin")
    assert args.psdu_file == "frame.bin"
    assert args.psdu_hex is None


def test_wifi_tx_frame_raw_sources_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse("wifi", "tx", "frame", "--type", "raw",
              "--psdu-hex", "8000", "--psdu-file", "frame.bin")


def test_wifi_tx_frame_requires_a_type():
    with pytest.raises(SystemExit):
        parse("wifi", "tx", "frame", "--ssid", "TEST")


def test_wifi_tx_stop_parses():
    assert parse("wifi", "tx", "stop").func.__name__ == "do_beacon_stop"


def test_wifi_tx_status_parses():
    assert parse("wifi", "tx", "status").func.__name__ == "do_tx_status"


def test_fm_rx_accepts_json():
    args = parse("fm", "rx", "--freq", "96.7", "--json")
    assert args.json is True


def test_fm_tx_requires_file():
    with pytest.raises(SystemExit):
        parse("fm", "tx", "--freq", "100.1")
    args = parse("fm", "tx", "--freq", "100.1", "--file", "track.mp3")
    assert args.file == "track.mp3"
    assert args.func.__name__ == "do_fm_tx"


def test_iq_rx_parses():
    args = parse("iq", "rx", "--out", "capture.bin")
    assert args.out == "capture.bin"
    assert args.bits == 12
    assert args.func.__name__ == "do_iq_rx"


def test_iq_tx_accepts_sample_rate():
    args = parse("iq", "tx", "--file", "capture.bin", "--sr", "2.1")
    assert args.sr == 2.1


def test_iq_tx_leaves_omitted_replay_settings_to_the_sidecar():
    args = parse("iq", "tx", "--file", "capture.bin")
    assert args.bits is None
    assert args.sr is None


def test_debug_commands_parse():
    assert parse("debug", "cpu").func.__name__ == "do_cpu"
    args = parse("debug", "diag", "--duration", "2")
    assert args.duration == 2.0
    assert args.func.__name__ == "do_diag"
    args = parse("debug", "stream", "--duration", "1")
    assert args.duration == 1.0
    assert args.func.__name__ == "do_stream"


@pytest.mark.parametrize(
    "argv",
    [
        ("mode", "wifi"),
        ("stream",),
        ("diag",),
        ("cpu",),
        ("caps",),
        ("beacon",),
        ("beacon-stop",),
        ("fm-tx", "--freq", "100.1", "--file", "track.mp3"),
        ("fm-rx", "--freq", "100.1"),
        ("rds", "--freq", "100.1"),
        ("iq-rx", "--out", "capture.bin"),
        ("iq-tx", "--file", "capture.bin"),
        ("iq-bits", "12"),
        ("fm", "--freq", "100.1", "--file", "track.mp3"),
    ],
)
def test_old_commands_are_gone(argv):
    with pytest.raises(SystemExit):
        parse(*argv)


@pytest.mark.parametrize(
    "argv",
    [
        ("fm", "rx", "--freq", "100.1", "--keep-mode"),
        ("fm", "rds", "--freq", "100.1", "--keep-mode"),
        ("iq", "rx", "--out", "capture.bin", "--keep-mode"),
    ],
)
def test_keep_mode_flag_is_gone(argv):
    with pytest.raises(SystemExit):
        parse(*argv)


@pytest.mark.parametrize(
    "argv",
    [
        ("radio", "freq", "0"),
        ("radio", "freq", "-1"),
        ("radio", "freq", "nan"),
        ("radio", "bw", "-1"),
        ("radio", "sample-rate", "-1"),
        ("viz", "fps", "0"),
        ("viz", "fps", "256"),
        ("viz", "capture", "--duration", "-1"),
        ("wifi", "scan", "--duration", "-1"),
        ("wifi", "tx", "beacon", "--rate", "5"),
        ("wifi", "tx", "beacon", "--rate", "55"),
        ("wifi", "tx", "beacon", "--count", "-1"),
        ("wifi", "tx", "beacon", "--count", "65536"),
        ("wifi", "tx", "beacon", "--interval", "-1"),
        ("wifi", "tx", "frame", "--type", "beacon", "--rate", "-1"),
        ("fm", "tx", "--freq", "100.1", "--file", "t.mp3", "--rate", "0"),
        ("fm", "tx", "--freq", "100.1", "--file", "t.mp3",
         "--rate", str(MAX_FM_AUDIO_RATE_HZ + 1)),
        ("iq", "rx", "--out", "x", "--duration", "-1"),
        ("iq", "tx", "--file", "x", "--sr", "-1"),
    ],
)
def test_numeric_arguments_out_of_wire_range_are_rejected(argv):
    with pytest.raises(SystemExit):
        parse(*argv)


def test_numeric_arguments_accept_the_wire_limits():
    assert parse("wifi", "tx", "beacon", "--rate", "54").rate == 54
    assert parse("wifi", "tx", "beacon", "--count", "65535").count == 65535
    assert parse("fm", "tx", "--freq", "100.1", "--file", "t.mp3",
                 "--rate", str(MAX_FM_AUDIO_RATE_HZ)).rate == \
        MAX_FM_AUDIO_RATE_HZ
