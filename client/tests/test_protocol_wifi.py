"""Tests for extended DATA_WIFI wire format parsing."""
import struct
from shesha.protocol import parse_wifi, WifiFrame, WifiMeta, WifiIeSummary


def _build_wifi_payload(
    ts: float = 1.0,
    rate_code: int = 0x0B,
    rate_mbps: int = 6,
    fcs_ok: bool = True,
    length: int = 100,
    frame_class: int = 3,  # beacon
    rx_gain: float = 50.0,
    rx_freq_hz: int = 5_180_000_000,
    psdu: bytes = b'',
    meta_flags: int = 0,
    bssid: bytes = b'\x00' * 6,
    sa: bytes = b'\x00' * 6,
    da: bytes = b'\x00' * 6,
    ssid: bytes = b'',
    ie_block: bytes | None = None,
) -> bytes:
    """Build a synthetic DATA_WIFI payload for testing."""
    # Fixed header (28 bytes)
    header = struct.pack('<dBBBHBfHQ',
                         ts, rate_code, rate_mbps, 1 if fcs_ok else 0,
                         length, frame_class, rx_gain, len(psdu),
                         rx_freq_hz)
    # Meta block
    ssid_len = len(ssid)
    meta = bytes([meta_flags]) + bssid + sa + da + bytes([ssid_len]) + ssid
    # IE block
    ie = ie_block if ie_block else b''
    return header + psdu + meta + ie


def test_minimal_frame_no_meta():
    """Frame with meta block all zeros."""
    payload = _build_wifi_payload(meta_flags=0)
    f = parse_wifi(payload)
    assert f.timestamp == 1.0
    assert f.rate_mbps == 6
    assert f.fcs_ok is True
    assert f.frame_class == 3
    assert f.frame_class_name == "beacon"
    assert f.rx_gain == 50.0
    assert f.rx_freq_hz == 5_180_000_000
    assert f.psdu == b''
    assert f.meta is not None
    assert f.meta.bssid == ""
    assert f.meta.ssid == ""
    assert f.ie is None


def test_beacon_with_full_meta_and_ie():
    """Beacon with BSSID, SA, DA, SSID, and IE summary."""
    bssid = b'\xaa\xbb\xcc\xdd\xee\x00'
    sa = b'\xaa\xbb\xcc\xdd\xee\x00'
    da = b'\xff\xff\xff\xff\xff\xff'
    ssid = b'TestNet'
    # meta_flags: has_bssid | has_sa | has_da | has_ssid | has_ie_summary
    meta_flags = 0x01 | 0x02 | 0x04 | 0x08 | 0x20

    # IE block (new layout):
    # channel=36, security=wpa3-sae(4), cipher=ccmp(3),
    # phy_flags=HT|VHT|VHT-80(0x0D), max_rate=866 LE (0x0362),
    # n_ss=2, chan_width=2(80MHz), center_chan=42, secondary_offset=1,
    # country=CA, pmf=required(2), legacy_rate=108
    import struct as _s
    ie_block = bytes([36, 4, 3, 0x0D]) + _s.pack('<H', 866) + bytes([
        2,  # n_ss
        2,  # chan_width (80MHz)
        42, # center_chan
        1,  # secondary_offset (above)
        ord('C'), ord('A'),  # country
        2,  # pmf (required)
        108,  # legacy_rate
    ])

    payload = _build_wifi_payload(
        frame_class=3, meta_flags=meta_flags,
        bssid=bssid, sa=sa, da=da, ssid=ssid,
        ie_block=ie_block,
    )
    f = parse_wifi(payload)

    assert f.frame_class_name == "beacon"
    assert f.meta is not None
    assert f.meta.bssid == "aa:bb:cc:dd:ee:00"
    assert f.meta.sa == "aa:bb:cc:dd:ee:00"
    assert f.meta.da == "ff:ff:ff:ff:ff:ff"
    assert f.meta.ssid == "TestNet"
    assert f.meta.is_eapol is False

    assert f.ie is not None
    assert f.ie.channel == 36
    assert f.ie.security == "wpa3-sae"
    assert f.ie.cipher == "ccmp"
    assert f.ie.phy == "VHT-80/HT"
    assert f.ie.max_rate == 866.0
    assert f.ie.n_ss == 2
    assert f.ie.chan_width == 2
    assert f.ie.center_chan == 42
    assert f.ie.secondary_offset == 1
    assert f.ie.country == "CA"
    assert f.ie.pmf == "required"


def test_data_frame_with_eapol():
    """Data frame with EAPOL flag set."""
    meta_flags = 0x01 | 0x02 | 0x04 | 0x10  # bssid + sa + da + eapol
    bssid = b'\x11\x22\x33\x44\x55\x66'
    sa = b'\xde\xad\xbe\xef\x00\x01'
    da = b'\x11\x22\x33\x44\x55\x66'

    payload = _build_wifi_payload(
        frame_class=8,  # eapol
        meta_flags=meta_flags,
        bssid=bssid, sa=sa, da=da,
    )
    f = parse_wifi(payload)

    assert f.frame_class_name == "eapol"
    assert f.meta is not None
    assert f.meta.bssid == "11:22:33:44:55:66"
    assert f.meta.sa == "de:ad:be:ef:00:01"
    assert f.meta.is_eapol is True
    assert f.ie is None


def test_frame_with_psdu():
    """Frame with PSDU bytes present."""
    psdu = b'\x80\x00' + b'\x00' * 22  # fake beacon FC + padding
    payload = _build_wifi_payload(
        psdu=psdu,
        meta_flags=0x01 | 0x02 | 0x04,
        bssid=b'\xaa\xbb\xcc\x11\x22\x33',
        sa=b'\xaa\xbb\xcc\x11\x22\x33',
        da=b'\xff\xff\xff\xff\xff\xff',
    )
    f = parse_wifi(payload)
    assert f.psdu == psdu
    assert f.meta is not None
    assert f.meta.bssid == "aa:bb:cc:11:22:33"


def test_fcs_fail_frame():
    """FCS-fail frame classified as HT/VHT."""
    payload = _build_wifi_payload(
        fcs_ok=False, frame_class=2, rate_mbps=6, length=23,
        meta_flags=0,
    )
    f = parse_wifi(payload)
    assert f.fcs_ok is False
    assert f.frame_class_name == "ht_vht"
    assert f.meta is not None
    assert f.meta.bssid == ""


def test_all_frame_class_names():
    """Verify all frame class indices map to names."""
    for i, name in enumerate(["ack", "block_ack", "ht_vht", "beacon", "mgmt",
                               "probe_req", "probe_resp", "data", "eapol",
                               "deauth", "disassoc", "unknown"]):
        payload = _build_wifi_payload(frame_class=i, meta_flags=0)
        f = parse_wifi(payload)
        assert f.frame_class_name == name


def test_deauth_and_disassoc_have_distinct_names():
    for index, name in ((9, "deauth"), (10, "disassoc")):
        f = parse_wifi(_build_wifi_payload(frame_class=index, meta_flags=0))
        assert f.frame_class_name == name
