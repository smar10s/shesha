"""WiFi scan data model: AP table, STA table, AP grouping.

Shared module used by both CLI and TUI for accumulating and organizing
scan results from the daemon's DATA_WIFI stream.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .channels import freq_to_channel

# Generous bounds for a real environment.  When a table fills, drop the
# entry that has gone quiet longest; the age-out pass normally handles it.
MAX_APS = 2048
MAX_STATIONS = 16384


@dataclass
class ApRecord:
    """One access point identified by BSSID."""
    bssid: str
    ssid: str = ""
    channel: int = 0
    security: str = "unknown"
    cipher: str = "unknown"
    pmf: str = "none"
    phy: str = ""
    country: str = ""
    max_rate: float = 0.0
    n_ss: int = 0
    chan_width: int = 0
    manufacturer: str = ""
    rx_gain: float = 0.0
    beacon_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    group_id: int = -1


@dataclass
class StaRecord:
    """One station (client) identified by MAC address."""
    mac: str
    bssid: str = ""
    channel: int = 0
    frame_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    rx_gain: float = 0.0
    probed_ssids: list[str] = field(default_factory=list)


class ScanState:
    """Accumulates scan results from a stream of WifiFrames.

    Call update() with each WifiFrame received from the daemon.
    Query aps, stations, and group_aps() for organized results.
    """

    def __init__(self) -> None:
        self.aps: dict[str, ApRecord] = {}
        self.stations: dict[str, StaRecord] = {}

    @staticmethod
    def _evict_oldest(records: dict) -> None:
        oldest = min(records, key=lambda key: records[key].last_seen)
        del records[oldest]

    def update(self, frame) -> list[str]:
        """Process one WifiFrame. Returns list of event strings."""
        events: list[str] = []
        meta = frame.meta
        if meta is None:
            return events

        ts = frame.timestamp
        bssid = meta.bssid
        fclass = frame.frame_class_name
        rx_channel = (freq_to_channel(frame.rx_freq_hz / 1e6)
                      if frame.rx_freq_hz else 0)

        # Track APs from beacons and probe responses
        if fclass in ("beacon", "probe_resp") and bssid:
            is_new = bssid not in self.aps
            if is_new and len(self.aps) >= MAX_APS:
                self._evict_oldest(self.aps)
            ap = self.aps.setdefault(bssid, ApRecord(bssid=bssid, first_seen=ts))
            ap.beacon_count += 1
            ap.last_seen = ts
            ap.rx_gain = frame.rx_gain

            if meta.ssid and (not ap.ssid or ap.ssid == "<hidden>"):
                ap.ssid = meta.ssid

            # Update from IE summary if available
            ie = frame.ie
            if ie is not None:
                if ie.channel:
                    ap.channel = ie.channel
                if ie.security != "unknown":
                    ap.security = ie.security
                if ie.cipher != "unknown":
                    ap.cipher = ie.cipher
                if ie.pmf != "none":
                    ap.pmf = ie.pmf
                if ie.phy:
                    ap.phy = ie.phy
                if ie.country:
                    ap.country = ie.country
                if ie.max_rate > 0:
                    ap.max_rate = ie.max_rate
                if ie.n_ss > 0:
                    ap.n_ss = ie.n_ss
                if ie.chan_width > 0:
                    ap.chan_width = ie.chan_width

            # Some beacons carry no DS Parameter Set.  The channel we heard
            # them on is still true and is better than no channel at all.
            if ap.channel == 0 and rx_channel:
                ap.channel = rx_channel

            if is_new:
                label = f'"{meta.ssid}"' if meta.ssid else "(hidden)"
                events.append(f"new AP {bssid} {label}")

        # Track stations from data, EAPOL, and probe request frames
        sa = meta.sa
        if (fclass in ("data", "eapol", "probe_req")
                and sa
                and sa != "ff:ff:ff:ff:ff:ff"):
            # For probe_req, bssid is broadcast — track station without AP association
            if fclass == "probe_req":
                if not sa.startswith("33:") and not sa.startswith("01:"):
                    is_new = sa not in self.stations
                    if is_new and len(self.stations) >= MAX_STATIONS:
                        self._evict_oldest(self.stations)
                    sta = self.stations.setdefault(sa, StaRecord(mac=sa, first_seen=ts))
                    sta.frame_count += 1
                    sta.last_seen = ts
                    sta.rx_gain = frame.rx_gain
                    if rx_channel:
                        sta.channel = rx_channel
                    # Accumulate probed SSIDs (cap at 10)
                    if meta.ssid and meta.ssid not in sta.probed_ssids:
                        if len(sta.probed_ssids) < 10:
                            sta.probed_ssids.append(meta.ssid)
                    if is_new:
                        events.append(f"new STA {sa}")
            elif bssid and sa != bssid and bssid != "ff:ff:ff:ff:ff:ff":
                # Skip multicast addresses
                if not sa.startswith("33:") and not sa.startswith("01:"):
                    is_new = sa not in self.stations
                    if is_new and len(self.stations) >= MAX_STATIONS:
                        self._evict_oldest(self.stations)
                    sta = self.stations.setdefault(sa, StaRecord(mac=sa, first_seen=ts))
                    sta.frame_count += 1
                    sta.last_seen = ts
                    sta.bssid = bssid
                    sta.rx_gain = frame.rx_gain
                    if rx_channel:
                        sta.channel = rx_channel
                    if is_new:
                        events.append(f"new STA {sa}")

        # EAPOL events
        if meta.is_eapol:
            sa_short = meta.sa[-8:] if meta.sa else "?"
            da_short = meta.da[-8:] if meta.da else "?"
            events.append(f"EAPOL {sa_short} \u2192 {da_short}")

        return events

    def group_aps(self) -> list[list[str]]:
        """Cluster AP BSSIDs by physical device.

        Heuristics (any match → same device):
        1. Bytes 2-6 identical (only first byte differs) — catches LA-bit VAPs
        2. Bytes 1-5 identical and last byte differs by ≤ 8 — catches multi-SSID increment
        3. Same OUI (bytes 1-3) and last 3 bytes differ by ≤ 16 — legacy heuristic

        Returns list of groups, each group is a sorted list of BSSIDs.
        Also assigns group_id to each ApRecord.
        """
        bssids = sorted(self.aps.keys())
        if not bssids:
            return []

        # Parse MACs to byte tuples once
        def _mac_bytes(mac: str) -> tuple[int, ...]:
            return tuple(int(b, 16) for b in mac.split(':'))

        parsed = {b: _mac_bytes(b) for b in bssids}

        # Union-Find
        parent: dict[str, str] = {b: b for b in bssids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        def _should_group(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
            # Heuristic 1: bytes 2-6 identical (first byte only differs)
            if a[1:] == b[1:]:
                return True
            # Heuristic 2: bytes 1-5 identical, last byte within 8
            if a[:5] == b[:5] and abs(a[5] - b[5]) <= 8:
                return True
            # Heuristic 3: same OUI, last-3-bytes within 16
            if a[:3] == b[:3]:
                tail_a = (a[3] << 16) | (a[4] << 8) | a[5]
                tail_b = (b[3] << 16) | (b[4] << 8) | b[5]
                if abs(tail_a - tail_b) <= 0x10:
                    return True
            return False

        # Compare all pairs (O(n^2) but n is small — typically <50 APs)
        for i in range(len(bssids)):
            for j in range(i + 1, len(bssids)):
                if _should_group(parsed[bssids[i]], parsed[bssids[j]]):
                    union(bssids[i], bssids[j])

        # Build groups
        groups_map: dict[str, list[str]] = {}
        for b in bssids:
            root = find(b)
            groups_map.setdefault(root, []).append(b)

        def _is_la(mac: str) -> bool:
            return bool(int(mac.split(':')[0], 16) & 0x02)

        def _sort_key(b: str) -> tuple[int, int, str]:
            # Primary: non-LA first (0 before 1)
            # Secondary: has real SSID first (0 before 1)
            la = 1 if _is_la(b) else 0
            has_ssid = 0 if self.aps[b].ssid else 1
            return (la, has_ssid, b)

        groups = [sorted(g, key=_sort_key) for g in groups_map.values()]
        groups.sort(key=lambda g: self.aps[g[0]].last_seen, reverse=True)

        # Assign group_id
        for gid, group in enumerate(groups):
            for b in group:
                self.aps[b].group_id = gid

        # Assign manufacturer from OUI lookup
        from .oui import manufacturer as _oui_lookup, _is_locally_administered
        for group in groups:
            mfr = ""
            for b in group:
                if not _is_locally_administered(b):
                    mfr = _oui_lookup(b)
                    if mfr:
                        break
            for b in group:
                self.aps[b].manufacturer = mfr

        return groups

    def age_out(self, max_age_sec: float = 300.0, now: float | None = None) -> None:
        """Remove entries not seen within max_age_sec."""
        if now is None:
            # Use the most recent timestamp we've seen
            all_ts = [ap.last_seen for ap in self.aps.values()]
            all_ts += [sta.last_seen for sta in self.stations.values()]
            if not all_ts:
                return
            now = max(all_ts)

        cutoff = now - max_age_sec
        stale_aps = [b for b, ap in self.aps.items() if ap.last_seen < cutoff]
        for b in stale_aps:
            del self.aps[b]

        stale_stas = [m for m, sta in self.stations.items() if sta.last_seen < cutoff]
        for m in stale_stas:
            del self.stations[m]
