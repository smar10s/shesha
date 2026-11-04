"""802.11 frame builders shared by the CLI and TUI.

Every builder returns a PSDU without the FCS. The daemon appends the FCS
when it builds the transmit waveform.
"""
from __future__ import annotations

import struct
import time
from collections.abc import Iterable

BROADCAST = b"\xff" * 6
DEFAULT_BSSID = b"\x02\x00\x00\x00\x00\x01"

_FC_BEACON = 0x0080
_FC_PROBE_REQ = 0x0040
_FC_PROBE_RESP = 0x0050
_FC_DATA_TO_DS = 0x0108
_FC_ACK = 0x00D4
_FC_DEAUTH = 0x00C0
_SUPPORTED_RATES = bytes([0x82, 0x84, 0x8b, 0x96, 0x0c, 0x12, 0x18, 0x24])
_DEFAULT_CAPABILITY = 0x0401
_TIM = b"\x00\x01\x00\x00"
_MAX_PSDU = 4096


def parse_mac(text: str) -> bytes:
    """Parse a colon-separated MAC address."""
    parts = text.split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError(f"invalid MAC address: {text!r}")
    try:
        return bytes(int(part, 16) for part in parts)
    except ValueError:
        raise ValueError(f"invalid MAC address: {text!r}") from None


def format_mac(addr: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in addr)


def _check_mac(addr: bytes, name: str) -> bytes:
    if len(addr) != 6:
        raise ValueError(f"{name} must be 6 bytes")
    return addr


def _ie(tag: int, data: bytes) -> bytes:
    if not 0 <= tag <= 0xFF:
        raise ValueError(f"IE tag out of range: {tag}")
    if len(data) > 0xFF:
        raise ValueError("IE data is longer than 255 bytes")
    return bytes([tag, len(data)]) + data


def _header(fc: int, da: bytes, sa: bytes, bssid: bytes, seq: int) -> bytes:
    if not 0 <= seq <= 0xFFF:
        raise ValueError(f"sequence number out of range: {seq}")
    return (struct.pack("<H", fc)
            + b"\x00\x00"
            + _check_mac(da, "DA")
            + _check_mac(sa, "SA")
            + _check_mac(bssid, "BSSID")
            + struct.pack("<H", seq << 4))


def _ap_body(ssid: str, channel: int, capability: int,
             beacon_interval: int, ies: Iterable[bytes],
             timestamp: int | None) -> bytes:
    encoded_ssid = ssid.encode("utf-8")
    if len(encoded_ssid) > 32:
        raise ValueError("SSID is longer than 32 bytes")
    if not 0 <= channel <= 0xFF:
        raise ValueError(f"channel out of range: {channel}")
    if not 0 <= capability <= 0xFFFF:
        raise ValueError(f"capability out of range: {capability}")
    if not 0 <= beacon_interval <= 0xFFFF:
        raise ValueError(f"beacon interval out of range: {beacon_interval}")
    if timestamp is None:
        timestamp = int(time.time() * 1_000_000) & 0xFFFFFFFFFFFFFFFF

    body = (_ie(0, encoded_ssid)
            + _ie(1, _SUPPORTED_RATES)
            + _ie(3, bytes([channel]))
            + _ie(5, _TIM))
    for ie in ies:
        body += ie

    return struct.pack("<QHH", timestamp, beacon_interval, capability) + body


def build_beacon(ssid: str = "SHESHA_TEST",
                 bssid: bytes = DEFAULT_BSSID,
                 da: bytes = BROADCAST,
                 sa: bytes | None = None,
                 channel: int = 6,
                 seq: int = 0,
                 capability: int = _DEFAULT_CAPABILITY,
                 beacon_interval: int = 100,
                 ies: Iterable[bytes] = (),
                 timestamp: int | None = None) -> bytes:
    """Build a beacon PSDU (without FCS)."""
    if sa is None:
        sa = bssid
    return (_header(_FC_BEACON, da, sa, bssid, seq)
            + _ap_body(ssid, channel, capability, beacon_interval, ies,
                       timestamp))


def build_probe_req(ssid: str = "",
                    sa: bytes = DEFAULT_BSSID,
                    da: bytes = BROADCAST,
                    bssid: bytes = BROADCAST,
                    seq: int = 0) -> bytes:
    """Build a probe request PSDU (without FCS)."""
    encoded_ssid = ssid.encode("utf-8")
    if len(encoded_ssid) > 32:
        raise ValueError("SSID is longer than 32 bytes")
    return _header(_FC_PROBE_REQ, da, sa, bssid, seq) + _ie(0, encoded_ssid)


def build_probe_resp(ssid: str = "SHESHA_TEST",
                     bssid: bytes = DEFAULT_BSSID,
                     da: bytes = BROADCAST,
                     sa: bytes | None = None,
                     channel: int = 6,
                     seq: int = 0,
                     capability: int = _DEFAULT_CAPABILITY,
                     beacon_interval: int = 100,
                     ies: Iterable[bytes] = (),
                     timestamp: int | None = None) -> bytes:
    """Build a probe response PSDU (without FCS)."""
    if sa is None:
        sa = bssid
    return (_header(_FC_PROBE_RESP, da, sa, bssid, seq)
            + _ap_body(ssid, channel, capability, beacon_interval, ies,
                       timestamp))


def build_data(da: bytes, sa: bytes, bssid: bytes, seq: int = 0) -> bytes:
    """Build a to-DS data PSDU (without FCS)."""
    return _header(_FC_DATA_TO_DS, da, sa, bssid, seq)


def build_ack(da: bytes = BROADCAST) -> bytes:
    """Build an ACK PSDU (without FCS)."""
    return struct.pack("<H", _FC_ACK) + b"\x00\x00" + _check_mac(da, "DA")


def build_deauth(da: bytes,
                 bssid: bytes,
                 sa: bytes | None = None,
                 reason: int = 7,
                 seq: int = 0) -> bytes:
    """Build a deauthentication PSDU (without FCS)."""
    if sa is None:
        sa = bssid
    if not 0 <= reason <= 0xFFFF:
        raise ValueError(f"reason code out of range: {reason}")
    return (_header(_FC_DEAUTH, da, sa, bssid, seq)
            + struct.pack("<H", reason))


def build_raw(psdu: bytes) -> bytes:
    """Validate and return a caller-supplied PSDU."""
    if not psdu:
        raise ValueError("PSDU is empty")
    if len(psdu) > _MAX_PSDU:
        raise ValueError(f"PSDU is longer than {_MAX_PSDU} bytes")
    return psdu
