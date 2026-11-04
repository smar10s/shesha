"""Byte-level MP3 fixtures for tag and duration tests.

These build the containers by hand so the tests do not depend on the
parser's own writer (or on a third-party tag library).
"""
from __future__ import annotations

import struct

FIELD_IDS = {
    2: {'title': 'TT2', 'artist': 'TP1', 'album': 'TAL', 'genre': 'TCO',
        'year': 'TYE', 'track': 'TRK'},
    3: {'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB', 'genre': 'TCON',
        'year': 'TDRC', 'track': 'TRCK'},
    4: {'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB', 'genre': 'TCON',
        'year': 'TDRC', 'track': 'TRCK'},
}


def syncsafe(n: int) -> bytes:
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F,
                  (n >> 7) & 0x7F, n & 0x7F))


def encode_text(text: str, encoding: int) -> bytes:
    if encoding == 0:
        return b'\x00' + text.encode('latin-1')
    if encoding == 1:
        return b'\x01' + text.encode('utf-16')
    if encoding == 2:
        return b'\x02' + text.encode('utf-16-be')
    return b'\x03' + text.encode('utf-8')


def text_frame(field: str, text: str, *, version: int = 3,
               encoding: int = 3) -> bytes:
    fid = FIELD_IDS[version][field]
    body = encode_text(text, encoding)
    if version == 2:
        return fid.encode('latin-1') + len(body).to_bytes(3, 'big') + body
    size = syncsafe(len(body)) if version == 4 else len(body).to_bytes(4, 'big')
    return fid.encode('latin-1') + size + b'\x00\x00' + body


def id3v2(fields: dict[str, str], *, version: int = 3,
          encoding: int = 3, padding: int = 0) -> bytes:
    frames = b''.join(text_frame(k, v, version=version, encoding=encoding)
                      for k, v in fields.items())
    frames += b'\x00' * padding
    header = b'ID3' + bytes([version, 0, 0])
    return header + syncsafe(len(frames)) + frames


def id3v1(*, title: str = '', artist: str = '', album: str = '',
          year: str = '', genre: int = 0) -> bytes:
    def pad(text: str, n: int) -> bytes:
        return text.encode('latin-1', 'replace')[:n].ljust(n, b'\x00')

    return (b'TAG' + pad(title, 30) + pad(artist, 30) + pad(album, 30)
            + pad(year, 4) + pad('', 30) + bytes([genre]))


MP3_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])  # MPEG1 Layer III, 128k, 44.1k
MP3_FRAME_LEN = 417


def mp3_frame(xing_frames: int | None = None) -> bytes:
    if xing_frames is None:
        return MP3_HEADER + b'\x00' * (MP3_FRAME_LEN - 4)
    frame = bytearray(MP3_HEADER + b'\x00' * (MP3_FRAME_LEN - 4))
    off = 4 + 32  # MPEG1 stereo side information
    frame[off:off + 4] = b'Xing'
    struct.pack_into('>I', frame, off + 4, 1)      # frames field present
    struct.pack_into('>I', frame, off + 8, xing_frames)
    return bytes(frame)


def audio(frames: int = 100, *, xing_frames: int | None = None) -> bytes:
    first = mp3_frame(xing_frames)
    rest = mp3_frame() * max(0, frames - 1)
    return first + rest


def write_tagged(path, fields: dict[str, str], *, version: int = 3,
                 encoding: int = 3, padding: int = 0) -> None:
    path.write_bytes(id3v2(fields, version=version, encoding=encoding,
                           padding=padding))


def write_mp3(path, fields: dict[str, str] | None = None, *,
              version: int = 3, encoding: int = 3, frames: int = 100,
              xing_frames: int | None = None, trailer: bytes | None = None,
              body: bytes | None = None) -> None:
    data = b''
    if fields is not None:
        data += id3v2(fields, version=version, encoding=encoding)
    data += body if body is not None else audio(frames, xing_frames=xing_frames)
    if trailer is not None:
        data += trailer
    path.write_bytes(data)
