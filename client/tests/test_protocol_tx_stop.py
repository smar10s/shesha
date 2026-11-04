"""CMD_TX_STOP carries a target protocol; 0xFF means every path."""
from shesha.protocol import (
    FRAME_HEADER, MsgType, TX_PROTO_IQ, TX_PROTO_WIFI, TX_STOP_ALL,
    TxProtocol, cmd_tx_stop,
)


def _payload(frame: bytes) -> bytes:
    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_TX_STOP
    assert plen == len(frame) - FRAME_HEADER.size
    return frame[FRAME_HEADER.size:]


def test_cmd_tx_stop_defaults_to_all_paths():
    assert TX_STOP_ALL == 0xFF
    assert _payload(cmd_tx_stop()) == bytes([TX_STOP_ALL])


def test_cmd_tx_stop_can_target_wifi():
    assert TX_PROTO_WIFI == TxProtocol.WIFI == 0
    assert _payload(cmd_tx_stop(TX_PROTO_WIFI)) == b"\x00"


def test_tx_protocol_has_an_iq_code():
    assert TX_PROTO_IQ == TxProtocol.IQ == 2
