"""Golden PSDU tests for the shared 802.11 frame builders."""
import pytest

from shesha.frames import (
    BROADCAST, build_ack, build_beacon, build_data, build_deauth,
    build_probe_req, build_probe_resp, build_raw, format_mac, parse_mac,
)

BSSID = bytes.fromhex("aabbccddeeff")
SA = bytes.fromhex("112233445566")


def test_build_beacon_golden_bytes():
    psdu = build_beacon(
        ssid="TEST",
        bssid=BSSID,
        channel=6,
        seq=0x123,
        capability=0x0411,
        beacon_interval=0x0064,
        timestamp=0x0102030405060708,
        ies=[bytes.fromhex("dd020102")],
    )

    assert psdu == bytes.fromhex(
        "8000"                  # frame control: beacon
        "0000"                  # duration
        "ffffffffffff"          # DA: broadcast
        "aabbccddeeff"          # SA: BSSID
        "aabbccddeeff"          # BSSID
        "3012"                  # sequence 0x123
        "0807060504030201"      # timestamp
        "6400"                  # beacon interval
        "1104"                  # capability
        "000454455354"          # SSID "TEST"
        "010882848b960c121824"  # supported rates
        "030106"                # DS parameter set, channel 6
        "050400010000"          # TIM
        "dd020102"              # extra IE
    )


def test_build_beacon_da_and_sa_override():
    da = bytes.fromhex("010203040506")
    psdu = build_beacon(
        ssid="X", bssid=BSSID, da=da, sa=SA, timestamp=0)

    assert psdu[4:10] == da
    assert psdu[10:16] == SA
    assert psdu[16:22] == BSSID


def test_build_deauth_golden_bytes():
    psdu = build_deauth(da=BSSID, sa=SA, bssid=BSSID, reason=7)

    assert psdu == bytes.fromhex(
        "c000"                  # frame control: deauth
        "0000"                  # duration
        "aabbccddeeff"          # DA
        "112233445566"          # SA
        "aabbccddeeff"          # BSSID
        "0000"                  # sequence
        "0700"                  # reason
    )


def test_build_deauth_defaults_sa_to_bssid():
    psdu = build_deauth(da=BROADCAST, bssid=BSSID, reason=3)

    assert psdu[10:16] == BSSID
    assert psdu[-2:] == b"\x03\x00"


def test_build_probe_req_golden_bytes():
    psdu = build_probe_req(ssid="TEST", sa=SA, seq=0)

    assert psdu == bytes.fromhex(
        "4000"                  # frame control: probe request
        "0000"                  # duration
        "ffffffffffff"          # DA: broadcast
        "112233445566"          # SA
        "ffffffffffff"          # BSSID: broadcast
        "0000"                  # sequence
        "000454455354"          # SSID "TEST"
    )


def test_build_probe_resp_golden_bytes():
    psdu = build_probe_resp(
        ssid="TEST",
        bssid=BSSID,
        channel=6,
        seq=0,
        capability=0x0411,
        beacon_interval=0x0064,
        timestamp=0,
    )

    assert psdu == bytes.fromhex(
        "5000"                  # frame control: probe response
        "0000"                  # duration
        "ffffffffffff"          # DA: broadcast
        "aabbccddeeff"          # SA: BSSID
        "aabbccddeeff"          # BSSID
        "0000"                  # sequence
        "0000000000000000"      # timestamp
        "6400"                  # beacon interval
        "1104"                  # capability
        "000454455354"          # SSID "TEST"
        "010882848b960c121824"  # supported rates
        "030106"                # DS parameter set, channel 6
        "050400010000"          # TIM
    )


def test_build_data_golden_bytes():
    psdu = build_data(da=BSSID, sa=SA, bssid=BSSID, seq=0)

    assert psdu == bytes.fromhex(
        "0801"                  # frame control: data, to-DS
        "0000"                  # duration
        "aabbccddeeff"          # DA
        "112233445566"          # SA
        "aabbccddeeff"          # BSSID
        "0000"                  # sequence
    )


def test_build_ack_golden_bytes():
    psdu = build_ack(da=BSSID)

    assert psdu == bytes.fromhex(
        "d400"                  # frame control: ACK
        "0000"                  # duration
        "aabbccddeeff"          # DA
    )


def test_build_raw_returns_the_psdu_unchanged():
    psdu = bytes.fromhex("80000000")
    assert build_raw(psdu) == psdu


@pytest.mark.parametrize("payload", [b"", b"\x00" * 4097])
def test_build_raw_rejects_bad_lengths(payload):
    with pytest.raises(ValueError):
        build_raw(payload)


def test_parse_mac_accepts_uppercase_and_round_trips():
    addr = parse_mac("AA:BB:CC:DD:EE:FF")
    assert addr == BSSID
    assert format_mac(addr) == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize(
    "text", ["", "aa:bb", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:gg", "aabbccddeeff"])
def test_parse_mac_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_mac(text)


def test_build_beacon_rejects_oversized_ssid():
    with pytest.raises(ValueError):
        build_beacon(ssid="X" * 33)
