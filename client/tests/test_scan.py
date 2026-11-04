"""Tests for the scan data model and AP grouping."""
from dataclasses import dataclass
from shesha.scan import ScanState, ApRecord, StaRecord


# --- Fake frame objects for testing (duck-typed to match WifiFrame) ---

@dataclass
class FakeMeta:
    bssid: str = ""
    sa: str = ""
    da: str = ""
    ssid: str = ""
    is_eapol: bool = False


@dataclass
class FakeIe:
    channel: int = 0
    security: str = "unknown"
    cipher: str = "unknown"
    phy_flags: int = 0
    phy: str = ""
    max_rate: float = 0.0
    n_ss: int = 0
    chan_width: int = 0
    country: str = ""
    pmf: str = "none"


@dataclass
class FakeFrame:
    timestamp: float = 1.0
    rate_mbps: int = 6
    fcs_ok: bool = True
    length: int = 300
    frame_class: int = 3
    frame_class_name: str = "beacon"
    rx_gain: float = 50.0
    rx_freq_hz: int = 0
    psdu: bytes = b''
    meta: FakeMeta | None = None
    ie: FakeIe | None = None


# --- AP tracking tests ---

def test_new_ap_from_beacon():
    state = ScanState()
    frame = FakeFrame(
        timestamp=10.0,
        frame_class_name="beacon",
        rx_gain=55.0,
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", ssid="Home"),
        ie=FakeIe(channel=36, security="wpa2-psk", cipher="ccmp", pmf="capable", country="CA"),
    )
    events = state.update(frame)
    assert len(events) == 1
    assert "new AP" in events[0]
    assert "aa:bb:cc:00:00:01" in events[0]
    assert '"Home"' in events[0]

    ap = state.aps["aa:bb:cc:00:00:01"]
    assert ap.ssid == "Home"
    assert ap.channel == 36
    assert ap.security == "wpa2-psk"
    assert ap.cipher == "ccmp"
    assert ap.pmf == "capable"
    assert ap.country == "CA"
    assert ap.beacon_count == 1
    assert ap.first_seen == 10.0
    assert ap.last_seen == 10.0
    assert ap.rx_gain == 55.0


def test_ap_update_increments():
    state = ScanState()
    meta = FakeMeta(bssid="aa:bb:cc:00:00:01", ssid="Home")
    ie = FakeIe(channel=36, security="wpa2-psk", cipher="ccmp")

    state.update(FakeFrame(timestamp=1.0, meta=meta, ie=ie))
    state.update(FakeFrame(timestamp=2.0, meta=meta, ie=ie, rx_gain=60.0))
    state.update(FakeFrame(timestamp=3.0, meta=meta, ie=ie))

    ap = state.aps["aa:bb:cc:00:00:01"]
    assert ap.beacon_count == 3
    assert ap.first_seen == 1.0
    assert ap.last_seen == 3.0
    assert ap.rx_gain == 50.0  # last frame's rx_gain


def test_hidden_ssid_updated_later():
    state = ScanState()
    state.update(FakeFrame(
        timestamp=1.0, meta=FakeMeta(bssid="aa:bb:cc:00:00:01", ssid=""),
    ))
    assert state.aps["aa:bb:cc:00:00:01"].ssid == ""

    state.update(FakeFrame(
        timestamp=2.0, meta=FakeMeta(bssid="aa:bb:cc:00:00:01", ssid="Revealed"),
    ))
    assert state.aps["aa:bb:cc:00:00:01"].ssid == "Revealed"


def test_probe_resp_creates_ap():
    state = ScanState()
    frame = FakeFrame(
        timestamp=5.0,
        frame_class_name="probe_resp",
        meta=FakeMeta(bssid="11:22:33:44:55:66", ssid="Guest"),
        ie=FakeIe(channel=1),
    )
    events = state.update(frame)
    assert any("new AP" in e for e in events)
    assert "11:22:33:44:55:66" in state.aps


# --- STA tracking tests ---

def test_sta_from_data_frame():
    state = ScanState()
    frame = FakeFrame(
        timestamp=10.0,
        frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01", da="aa:bb:cc:00:00:01"),
    )
    events = state.update(frame)
    assert any("new STA" in e for e in events)

    sta = state.stations["de:ad:be:ef:00:01"]
    assert sta.bssid == "aa:bb:cc:00:00:01"
    assert sta.frame_count == 1
    assert sta.first_seen == 10.0


def test_sta_channel_from_receive_frequency():
    state = ScanState()
    state.update(FakeFrame(
        timestamp=10.0,
        frame_class_name="data",
        rx_freq_hz=2_437_000_000,
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01",
                      da="aa:bb:cc:00:00:01"),
    ))
    assert state.stations["de:ad:be:ef:00:01"].channel == 6


def test_sta_channel_follows_latest_receive_frequency():
    state = ScanState()
    meta = FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01",
                    da="aa:bb:cc:00:00:01")
    state.update(FakeFrame(timestamp=1.0, frame_class_name="data",
                           rx_freq_hz=2_437_000_000, meta=meta))
    state.update(FakeFrame(timestamp=2.0, frame_class_name="data",
                           rx_freq_hz=5_180_000_000, meta=meta))
    assert state.stations["de:ad:be:ef:00:01"].channel == 36


def test_ap_channel_falls_back_to_receive_frequency():
    state = ScanState()
    state.update(FakeFrame(
        timestamp=1.0,
        frame_class_name="beacon",
        rx_freq_hz=2_462_000_000,
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", ssid="Home"),
        ie=FakeIe(channel=0),
    ))
    assert state.aps["aa:bb:cc:00:00:01"].channel == 11


def test_multicast_sa_filtered():
    state = ScanState()
    # 33:xx multicast
    frame = FakeFrame(
        timestamp=1.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="33:33:00:00:00:01"),
    )
    state.update(frame)
    assert "33:33:00:00:00:01" not in state.stations

    # 01:xx multicast
    frame2 = FakeFrame(
        timestamp=2.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="01:00:5e:00:00:01"),
    )
    state.update(frame2)
    assert "01:00:5e:00:00:01" not in state.stations


def test_broadcast_da_not_sta():
    """SA == BSSID should not be tracked as station."""
    state = ScanState()
    frame = FakeFrame(
        timestamp=1.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="aa:bb:cc:00:00:01", da="ff:ff:ff:ff:ff:ff"),
    )
    state.update(frame)
    assert "aa:bb:cc:00:00:01" not in state.stations


# --- EAPOL events ---

def test_eapol_event():
    state = ScanState()
    frame = FakeFrame(
        timestamp=1.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01",
                      da="aa:bb:cc:00:00:01", is_eapol=True),
    )
    events = state.update(frame)
    assert any("EAPOL" in e for e in events)


# --- AP grouping tests ---

def test_group_sequential_macs():
    """4 BSSIDs with same OUI and sequential last bytes -> 1 group."""
    state = ScanState()
    for i in range(4):
        bssid = f"aa:bb:cc:00:00:{i:02x}"
        state.aps[bssid] = ApRecord(bssid=bssid, last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1
    assert len(groups[0]) == 4


def test_group_different_ouis():
    """2 BSSIDs with different OUIs -> 2 groups."""
    state = ScanState()
    state.aps["aa:bb:cc:00:00:01"] = ApRecord(bssid="aa:bb:cc:00:00:01", last_seen=10.0)
    state.aps["11:22:33:00:00:01"] = ApRecord(bssid="11:22:33:00:00:01", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 2


def test_group_far_apart_same_oui():
    """Same OUI but last bytes differ by >16 -> separate groups."""
    state = ScanState()
    state.aps["aa:bb:cc:00:00:00"] = ApRecord(bssid="aa:bb:cc:00:00:00", last_seen=10.0)
    state.aps["aa:bb:cc:00:01:00"] = ApRecord(bssid="aa:bb:cc:00:01:00", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 2


def test_group_transitive():
    """A close to B, B close to C -> all in one group (transitive)."""
    state = ScanState()
    # 0x00, 0x08, 0x10: each pair within 16, but 0x00 to 0x10 = 16 (still within)
    state.aps["aa:bb:cc:00:00:00"] = ApRecord(bssid="aa:bb:cc:00:00:00", last_seen=10.0)
    state.aps["aa:bb:cc:00:00:08"] = ApRecord(bssid="aa:bb:cc:00:00:08", last_seen=10.0)
    state.aps["aa:bb:cc:00:00:10"] = ApRecord(bssid="aa:bb:cc:00:00:10", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_group_assigns_ids():
    """group_id is assigned to each AP."""
    state = ScanState()
    state.aps["aa:bb:cc:00:00:01"] = ApRecord(bssid="aa:bb:cc:00:00:01", last_seen=10.0)
    state.aps["aa:bb:cc:00:00:02"] = ApRecord(bssid="aa:bb:cc:00:00:02", last_seen=10.0)
    state.aps["11:22:33:00:00:01"] = ApRecord(bssid="11:22:33:00:00:01", last_seen=5.0)

    groups = state.group_aps()
    # Both aa:bb:cc should have same group_id
    assert state.aps["aa:bb:cc:00:00:01"].group_id == state.aps["aa:bb:cc:00:00:02"].group_id
    # 11:22:33 should have different group_id
    assert state.aps["11:22:33:00:00:01"].group_id != state.aps["aa:bb:cc:00:00:01"].group_id


def test_group_first_byte_difference_la_bit():
    """BSSIDs differing only in first byte (LA bit set) -> same group.

    Real-world: 10:78:5b:fd:b6:26 and 0e:78:5b:fd:b6:26 are the same AP.
    """
    state = ScanState()
    state.aps["10:78:5b:fd:b6:26"] = ApRecord(bssid="10:78:5b:fd:b6:26", last_seen=10.0)
    state.aps["0e:78:5b:fd:b6:26"] = ApRecord(bssid="0e:78:5b:fd:b6:26", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_group_last_byte_increment():
    """BSSIDs differing only in last byte by small amount -> same group."""
    state = ScanState()
    state.aps["aa:bb:cc:dd:ee:01"] = ApRecord(bssid="aa:bb:cc:dd:ee:01", last_seen=10.0)
    state.aps["aa:bb:cc:dd:ee:05"] = ApRecord(bssid="aa:bb:cc:dd:ee:05", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1


def test_group_unrelated_macs_separate():
    """Completely unrelated MACs stay separate."""
    state = ScanState()
    state.aps["10:78:5b:fd:b6:26"] = ApRecord(bssid="10:78:5b:fd:b6:26", last_seen=10.0)
    state.aps["ac:84:c6:11:22:33"] = ApRecord(bssid="ac:84:c6:11:22:33", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 2


def test_group_primary_prefers_non_la_bit():
    """Group primary (index 0) should be the non-LA-bit BSSID."""
    state = ScanState()
    # LA-bit set: 0e has bit 1 of first byte set (0x0e & 0x02 = true)
    state.aps["0e:78:5b:fd:b6:26"] = ApRecord(bssid="0e:78:5b:fd:b6:26", ssid="MyNet", last_seen=10.0)
    # Non-LA: 10 (0x10 & 0x02 = false)
    state.aps["10:78:5b:fd:b6:26"] = ApRecord(bssid="10:78:5b:fd:b6:26", ssid="", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1
    # Non-LA BSSID should be first (primary), even though it has no SSID
    assert groups[0][0] == "10:78:5b:fd:b6:26"


def test_group_primary_falls_back_to_ssid():
    """If all BSSIDs are non-LA, prefer the one with a real SSID."""
    state = ScanState()
    state.aps["10:78:5b:fd:b6:26"] = ApRecord(bssid="10:78:5b:fd:b6:26", ssid="", last_seen=10.0)
    state.aps["10:78:5b:fd:b6:27"] = ApRecord(bssid="10:78:5b:fd:b6:27", ssid="RealName", last_seen=10.0)

    groups = state.group_aps()
    assert len(groups) == 1
    assert groups[0][0] == "10:78:5b:fd:b6:27"


# --- Age out tests ---

def test_age_out_removes_stale():
    state = ScanState()
    state.aps["aa:bb:cc:00:00:01"] = ApRecord(bssid="aa:bb:cc:00:00:01", last_seen=1.0)
    state.aps["aa:bb:cc:00:00:02"] = ApRecord(bssid="aa:bb:cc:00:00:02", last_seen=100.0)
    state.stations["de:ad:be:ef:00:01"] = StaRecord(mac="de:ad:be:ef:00:01", last_seen=2.0)

    state.age_out(max_age_sec=50.0, now=100.0)

    assert "aa:bb:cc:00:00:01" not in state.aps
    assert "aa:bb:cc:00:00:02" in state.aps
    assert "de:ad:be:ef:00:01" not in state.stations


def test_no_meta_returns_empty_events():
    state = ScanState()
    frame = FakeFrame(meta=None)
    events = state.update(frame)
    assert events == []


# --- STA tracking: probe request ---

def test_probe_request_does_not_overwrite_bssid():
    """Probe requests with broadcast BSSID should not poison STA association."""
    state = ScanState()
    # First: data frame associates the STA correctly
    state.update(FakeFrame(
        timestamp=1.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01"),
    ))
    assert state.stations["de:ad:be:ef:00:01"].bssid == "aa:bb:cc:00:00:01"

    # Then: probe request with broadcast BSSID should NOT overwrite
    state.update(FakeFrame(
        timestamp=2.0, frame_class_name="probe_req",
        meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="de:ad:be:ef:00:01"),
    ))
    assert state.stations["de:ad:be:ef:00:01"].bssid == "aa:bb:cc:00:00:01"


def test_probe_only_sta_is_unassociated():
    """STA seen only in probe requests has empty bssid (unassociated)."""
    state = ScanState()
    state.update(FakeFrame(
        timestamp=1.0, frame_class_name="probe_req",
        meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="11:22:33:44:55:66"),
    ))
    # STA should be created from probe request
    assert "11:22:33:44:55:66" in state.stations
    assert state.stations["11:22:33:44:55:66"].bssid == ""


def test_sta_only_tracked_from_data_eapol_probe():
    """STA tracking fires for data, eapol, and probe_req frames only."""
    state = ScanState()
    # mgmt frame (not beacon/probe_resp) with valid sa and bssid
    state.update(FakeFrame(
        timestamp=1.0, frame_class_name="mgmt",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01"),
    ))
    assert "de:ad:be:ef:00:01" not in state.stations

    # data frame should work
    state.update(FakeFrame(
        timestamp=2.0, frame_class_name="data",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="de:ad:be:ef:00:01"),
    ))
    assert "de:ad:be:ef:00:01" in state.stations

    # eapol should also work
    state.update(FakeFrame(
        timestamp=3.0, frame_class_name="eapol",
        meta=FakeMeta(bssid="aa:bb:cc:00:00:01", sa="fe:ed:ca:fe:00:01",
                      is_eapol=True),
    ))
    assert "fe:ed:ca:fe:00:01" in state.stations


def test_probe_request_creates_station():
    """Probe request creates a station entry."""
    state = ScanState()
    frame = FakeFrame(
        timestamp=5.0,
        frame_class_name="probe_req",
        rx_gain=42.0,
        meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="ab:cd:ef:00:11:22",
                      ssid="MyNetwork"),
    )
    events = state.update(frame)
    assert any("new STA" in e for e in events)
    sta = state.stations["ab:cd:ef:00:11:22"]
    assert sta.bssid == ""
    assert sta.frame_count == 1
    assert sta.rx_gain == 42.0
    assert sta.probed_ssids == ["MyNetwork"]


def test_probe_ssid_accumulates_no_duplicates():
    """Probed SSIDs accumulate without duplicates."""
    state = ScanState()
    for i in range(5):
        state.update(FakeFrame(
            timestamp=float(i),
            frame_class_name="probe_req",
            rx_gain=30.0,
            meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="ab:cd:ef:00:11:22",
                          ssid="HomeWifi"),
        ))
    sta = state.stations["ab:cd:ef:00:11:22"]
    assert sta.probed_ssids == ["HomeWifi"]
    assert sta.frame_count == 5


def test_probe_ssid_cap_at_10():
    """Probed SSIDs cap at 10 entries."""
    state = ScanState()
    for i in range(15):
        state.update(FakeFrame(
            timestamp=float(i),
            frame_class_name="probe_req",
            rx_gain=30.0,
            meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="ab:cd:ef:00:11:22",
                          ssid=f"Net{i}"),
        ))
    sta = state.stations["ab:cd:ef:00:11:22"]
    assert len(sta.probed_ssids) == 10
    # First 10 should be there
    assert sta.probed_ssids == [f"Net{i}" for i in range(10)]


def test_probe_empty_ssid_not_added():
    """Empty SSID from probe request is not added to probed_ssids."""
    state = ScanState()
    state.update(FakeFrame(
        timestamp=1.0,
        frame_class_name="probe_req",
        rx_gain=30.0,
        meta=FakeMeta(bssid="ff:ff:ff:ff:ff:ff", sa="ab:cd:ef:00:11:22",
                      ssid=""),
    ))
    sta = state.stations["ab:cd:ef:00:11:22"]
    assert sta.probed_ssids == []


def test_ap_table_is_capped(monkeypatch):
    monkeypatch.setattr('shesha.scan.MAX_APS', 2)
    state = ScanState()
    state.aps['aa:bb:cc:00:00:01'] = ApRecord(
        bssid='aa:bb:cc:00:00:01', last_seen=10.0)
    state.aps['aa:bb:cc:00:00:02'] = ApRecord(
        bssid='aa:bb:cc:00:00:02', last_seen=20.0)

    frame = FakeFrame(
        timestamp=30.0,
        frame_class_name='beacon',
        meta=FakeMeta(bssid='aa:bb:cc:00:00:03', ssid='New'),
        ie=FakeIe(channel=36),
    )
    state.update(frame)

    assert len(state.aps) == 2
    assert 'aa:bb:cc:00:00:01' not in state.aps
    assert 'aa:bb:cc:00:00:03' in state.aps


def test_station_table_is_capped(monkeypatch):
    monkeypatch.setattr('shesha.scan.MAX_STATIONS', 2)
    state = ScanState()
    state.stations['aa:bb:cc:00:00:01'] = StaRecord(
        mac='aa:bb:cc:00:00:01', last_seen=10.0)
    state.stations['aa:bb:cc:00:00:02'] = StaRecord(
        mac='aa:bb:cc:00:00:02', last_seen=20.0)

    frame = FakeFrame(
        timestamp=30.0,
        frame_class_name='data',
        meta=FakeMeta(sa='aa:bb:cc:00:00:03', bssid='aa:bb:cc:00:00:10'),
    )
    state.update(frame)

    assert len(state.stations) == 2
    assert 'aa:bb:cc:00:00:01' not in state.stations
    assert 'aa:bb:cc:00:00:03' in state.stations
