"""Unit tests for the beacon frame builder."""
from shesha.frames import build_beacon


def test_build_beacon_structure():
    psdu = build_beacon(ssid="TEST", channel=1)
    # Frame control bytes (beacon = 0x80, 0x00)
    assert psdu[0:2] == b'\x80\x00'
    # Duration = 0
    assert psdu[2:4] == b'\x00\x00'
    # DA is broadcast
    assert psdu[4:10] == b'\xff' * 6
    # SA == BSSID (default)
    assert psdu[10:16] == psdu[16:22]
    # Fixed fields start at offset 24, length 12 -> IEs at 36
    ie_start = 36
    # SSID IE: tag=0, len=4, "TEST"
    assert psdu[ie_start] == 0
    assert psdu[ie_start + 1] == 4
    assert psdu[ie_start + 2:ie_start + 6] == b'TEST'


def test_build_beacon_default_ssid():
    psdu = build_beacon()
    ie_start = 36
    ssid_len = psdu[ie_start + 1]
    ssid = psdu[ie_start + 2:ie_start + 2 + ssid_len].decode()
    assert ssid == "SHESHA_TEST"


def test_build_beacon_channel_ie():
    psdu = build_beacon(ssid="X", channel=11)
    # Find DS Parameter Set IE (tag=3) after SSID and Supported Rates
    ie_start = 36
    pos = ie_start
    while pos + 2 <= len(psdu):
        tag = psdu[pos]
        tag_len = psdu[pos + 1]
        if tag == 3:  # DS Parameter Set
            assert tag_len == 1
            assert psdu[pos + 2] == 11
            return
        pos += 2 + tag_len
    assert False, "DS Parameter Set IE not found"


def test_build_beacon_custom_bssid():
    bssid = b'\xaa\xbb\xcc\xdd\xee\xff'
    psdu = build_beacon(ssid="X", bssid=bssid)
    assert psdu[10:16] == bssid  # SA
    assert psdu[16:22] == bssid  # BSSID
