import re
from pathlib import Path

import shesha.protocol as protocol
from shesha.protocol import FRAME_HEADER, TX_REPEAT_FOREVER, MsgType, TxFlags

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "protocol.md"
MAIN_C = ROOT / "firmware" / "src" / "main.c"
TX_FLAGS_H = ROOT / "firmware" / "src" / "tx_flags.h"
WIFI_SCAN_H = ROOT / "firmware" / "src" / "wifi_scan.h"

ROW = re.compile(r"^\| `0x([0-9A-Fa-f]{2})` \| `([A-Z0-9_]+)` \|", re.M)


def _documented():
    return {name: int(hex_id, 16) for hex_id, name in ROW.findall(DOC.read_text())}


def _section(title: str) -> str:
    text = DOC.read_text()
    start = text.index(title)
    rest = text[start + len(title):]
    end = re.search(r"\n#{2,4} ", rest)
    return rest[:end.start()] if end else rest


def _c_define(source: str, name: str) -> int:
    m = re.search(rf"^#define\s+{name}\s+(0x[0-9A-Fa-f]+|\d+)\b", source, re.M)
    assert m, f"{name} not defined"
    return int(m.group(1), 0)


def _cmd_case(source: str, name: str) -> str:
    m = re.search(
        rf"case {name}:\s*(.*?)(?=\n\s*case [A-Z_]+:|\n\s*default:)",
        source,
        re.S,
    )
    assert m, f"case {name} not found"
    return m.group(1)


def _ok_builders(source: str, name: str) -> set[str]:
    body = _cmd_case(source, name)
    return {
        "proto_build_ok" + (suffix or "")
        for suffix in re.findall(r"proto_build_ok(_u32_pair|_u32|_u64)?\(", body)
    }


def test_catalog_matches_message_types():
    expected = {name: int(member) for name, member in MsgType.__members__.items()}
    assert _documented() == expected


def test_header_size_matches_code():
    assert FRAME_HEADER.size == 9
    assert "9-byte header" in DOC.read_text()


def test_normative_code_list_names_every_wire_definition():
    section = _section("## Normative code")
    required = [
        "firmware/src/protocol.h",
        "firmware/src/protocol.c",
        "firmware/src/tx_flags.h",
        "firmware/src/fm_protocol.h",
        "firmware/src/wifi_protocol.h",
        "firmware/src/main.c",
        "client/shesha/protocol.py",
        "client/c/src/protocol_internal.h",
    ]
    for rel in required:
        assert f"`{rel}`" in section, rel
        assert (ROOT / rel).exists(), rel


def test_tx_flag_bits_match_firmware_and_docs():
    source = TX_FLAGS_H.read_text()
    assert TxFlags.NONE == 0
    assert TxFlags.CYCLIC == _c_define(source, "TX_FLAG_CYCLIC")
    assert TxFlags.STREAM == _c_define(source, "TX_FLAG_STREAM")
    assert TX_REPEAT_FOREVER == _c_define(source, "TX_REPEAT_FOREVER")

    section = _section("## CMD_TX payload").lower()
    assert "bit 0 = cyclic" in section
    assert "bit 1 = stream" in section


def test_cmd_tx_flag_constraints_match_handlers_and_docs():
    main = MAIN_C.read_text()
    assert "if (flags & ~TX_FLAG_CYCLIC)" in main
    assert "if (flags & ~TX_FLAG_STREAM)" in main

    section = _section("## CMD_TX payload")
    wifi = re.search(r"\| WiFi \(0\) \| ([^|]+) \|", section)
    fm = re.search(r"\| FM \(3\) \| ([^|]+) \|", section)
    assert wifi and "CYCLIC" in wifi.group(1)
    assert fm and "STREAM" in fm.group(1)
    assert "CMD_IQ_TX_STREAM_DATA" in section
    assert "LoRa" in section


def test_resp_ok_variants_match_handlers_and_docs():
    main = MAIN_C.read_text()
    assert _ok_builders(main, "CMD_SET_FREQ") == {"proto_build_ok_u64"}
    assert _ok_builders(main, "CMD_SET_BW") == {"proto_build_ok_u32_pair"}
    assert _ok_builders(main, "CMD_SET_SAMPLE_RATE") == {"proto_build_ok_u32"}
    assert _ok_builders(main, "CMD_SET_MODE") == {"proto_build_ok_u32_pair"}

    section = _section("#### RESP_OK variants")
    rows = {
        name: fields
        for name, fields in re.findall(
            r"^\| `(CMD_[A-Z0-9_]+)` \| ([^|]+) \|", section, re.M
        )
    }
    assert "u64" in rows["CMD_SET_FREQ"]
    assert rows["CMD_SET_BW"].count("u32") == 2
    assert rows["CMD_SET_SAMPLE_RATE"].count("u32") == 1
    assert rows["CMD_SET_MODE"].count("u32") == 2


def test_data_wifi_layout_matches_parser_and_docs():
    assert protocol._WIFI_HEADER.size == 28

    source = WIFI_SCAN_H.read_text()
    meta_flags = {
        name: int(value, 0)
        for name, value in re.findall(
            r"^#define\s+(WIFI_META_\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b",
            source,
            re.M,
        )
    }
    assert meta_flags["WIFI_META_HAS_IE_SUMMARY"] == 0x20

    section = _section("### DATA_WIFI layout")
    for value in meta_flags.values():
        assert f"`0x{value:02x}`" in section.lower(), hex(value)
    assert "28 + psdu_len" in section
    assert "20 + ssid_len" in section
    assert "14-byte IE summary" in section
    assert "always" in section.lower()
