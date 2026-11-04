"""build_frame enforces the same payload bound as the daemon."""
import pytest

from shesha.protocol import FRAME_HEADER, MAX_PAYLOAD, MsgType, build_frame


def test_build_frame_accepts_a_payload_at_the_limit():
    payload = b'x' * MAX_PAYLOAD

    frame = build_frame(MsgType.CMD_STATUS, payload)

    msg_type, length, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert msg_type == MsgType.CMD_STATUS
    assert length == MAX_PAYLOAD
    assert len(frame) == FRAME_HEADER.size + MAX_PAYLOAD


def test_build_frame_rejects_an_oversized_payload():
    with pytest.raises(ValueError, match="payload"):
        build_frame(MsgType.CMD_STATUS, b'x' * (MAX_PAYLOAD + 1))
