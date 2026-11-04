"""MP3 metadata: ID3 tags and duration, without a tag library.

Only the frames the FM TX path uses are decoded: title, artist, album,
genre, year and track.  ID3v2.2, 2.3 and 2.4 are read, with an ID3v1
trailer as the fallback.  Duration comes from the MP3 frame headers, using
the Xing/Info frame count when present and the CBR bitrate otherwise.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ID3v1 genre indices.  The first 80 are the 1998 standard; 80..147 are the
# Winamp extensions nearly every tagger writes.  Unknown indices are passed
# through as their numeric string rather than guessed at.
ID3V1_GENRES = (
    'Blues', 'Classic Rock', 'Country', 'Dance', 'Disco', 'Funk', 'Grunge',
    'Hip-Hop', 'Jazz', 'Metal', 'New Age', 'Oldies', 'Other', 'Pop', 'R&B',
    'Rap', 'Reggae', 'Rock', 'Techno', 'Industrial', 'Alternative', 'Ska',
    'Death Metal', 'Pranks', 'Soundtrack', 'Euro-Techno', 'Ambient',
    'Trip-Hop', 'Vocal', 'Jazz+Funk', 'Fusion', 'Trance', 'Classical',
    'Instrumental', 'Acid', 'House', 'Game', 'Sound Clip', 'Gospel', 'Noise',
    'AlternRock', 'Bass', 'Soul', 'Punk', 'Space', 'Meditative',
    'Instrumental Pop', 'Instrumental Rock', 'Ethnic', 'Gothic', 'Darkwave',
    'Techno-Industrial', 'Electronic', 'Pop-Folk', 'Eurodance', 'Dream',
    'Southern Rock', 'Comedy', 'Cult', 'Gangsta', 'Top 40', 'Christian Rap',
    'Pop/Funk', 'Jungle', 'Native American', 'Cabaret', 'New Wave',
    'Psychadelic', 'Rave', 'Showtunes', 'Trailer', 'Lo-Fi', 'Tribal',
    'Acid Punk', 'Acid Jazz', 'Polka', 'Retro', 'Musical', 'Rock & Roll',
    'Hard Rock', 'Folk', 'Folk-Rock', 'National Folk', 'Swing', 'Fast Fusion',
    'Bebob', 'Latin', 'Revival', 'Celtic', 'Bluegrass', 'Avantgarde',
    'Gothic Rock', 'Progressive Rock', 'Psychedelic Rock', 'Symphonic Rock',
    'Slow Rock', 'Big Band', 'Chorus', 'Easy Listening', 'Acoustic', 'Humour',
    'Speech', 'Chanson', 'Opera', 'Chamber Music', 'Sonata', 'Symphony',
    'Booty Bass', 'Primus', 'Porn Groove', 'Satire', 'Slow Jam', 'Club',
    'Tango', 'Samba', 'Folklore', 'Ballad', 'Power Ballad', 'Rhythmic Soul',
    'Freestyle', 'Duet', 'Punk Rock', 'Drum Solo', 'A capella', 'Euro-House',
    'Dance Hall', 'Goa', 'Drum & Bass', 'Club-House', 'Hardcore', 'Terror',
    'Indie', 'BritPop', 'Negerpunk', 'Polsk Punk', 'Beat',
    'Christian Gangsta Rap', 'Heavy Metal', 'Black Metal', 'Crossover',
    'Contemporary Christian', 'Christian Rock', 'Merengue', 'Salsa',
    'Thrash Metal', 'Anime', 'JPop', 'Synthpop',
)

# v2.2 3-character IDs -> the v2.3/2.4 name used internally.
_V22_IDS = {
    'TT2': 'TIT2', 'TP1': 'TPE1', 'TAL': 'TALB',
    'TCO': 'TCON', 'TYE': 'TYER', 'TRK': 'TRCK',
}

_WANTED = frozenset({'TIT2', 'TPE1', 'TALB', 'TCON', 'TDRC', 'TYER', 'TRCK'})

# MPEG audio bitrate tables (kbit/s), Layer III only.
_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}
# Version bits: 0 is MPEG2.5, 2 is MPEG2, 3 is MPEG1.  Layer III uses 72
# samples-per-frame slots at MPEG2/2.5 and 144 at MPEG1.
_FRAME_COEFF = {3: 144, 2: 72, 0: 72}
_SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}


@dataclass(frozen=True)
class TrackTags:
    """Metadata a file advertises about itself.  Missing fields are ''."""

    title: str = ''
    artist: str = ''
    album: str = ''
    genre: str = ''
    year: str = ''
    track: str = ''

    @property
    def empty(self) -> bool:
        return not any((self.title, self.artist, self.album, self.genre,
                        self.year, self.track))


def normalize_genre(raw: str) -> str:
    """Turn a raw genre value into a name.

    ID3v1 (and some ID3v2 tags) store the genre as an index, sometimes
    parenthesised.  Known indices become the standard name; anything else is
    returned unchanged.
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    match = re.match(r'^\((\d+)\)(.*)$', raw)
    if match:
        refinement = match.group(2).strip()
        if refinement:
            return refinement
        index = int(match.group(1))
        if 0 <= index < len(ID3V1_GENRES):
            return ID3V1_GENRES[index]
        return str(index)
    digits = raw.strip('()')
    if digits.isdigit():
        index = int(digits)
        if 0 <= index < len(ID3V1_GENRES):
            return ID3V1_GENRES[index]
    return raw


def _syncsafe(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def _unsync(data: bytes) -> bytes:
    return data.replace(b'\xff\x00', b'\xff')


def _decode_text(payload: bytes) -> str:
    if not payload:
        return ''
    encoding, raw = payload[0], payload[1:]
    try:
        if encoding == 0:
            text = raw.decode('latin-1')
        elif encoding == 1:
            text = raw.decode('utf-16')
        elif encoding == 2:
            text = raw.decode('utf-16-be')
        else:
            text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('latin-1', 'replace')
    return text.split('\x00', 1)[0].strip('\x00')


def _read_id3v2(path: str) -> tuple[dict[str, str] | None, int]:
    """Return (frames, audio_offset) for an ID3v2 tag, or (None, 0)."""
    try:
        with open(path, 'rb') as f:
            header = f.read(10)
            if len(header) < 10 or header[:3] != b'ID3':
                return None, 0
            version = header[3]
            if version not in (2, 3, 4):
                return None, 0
            flags = header[5]
            size = _syncsafe(header[6:10])
            data = f.read(size)
    except OSError:
        return None, 0

    if len(data) < size:
        return None, 0
    if flags & 0x80:
        data = _unsync(data)

    pos = 0
    if flags & 0x40:
        if len(data) < 4:
            return None, 0
        if version == 4:
            ext = _syncsafe(data[:4])
        else:
            ext = int.from_bytes(data[:4], 'big') + 4
        pos = ext

    frames: dict[str, str] = {}
    while pos + (6 if version == 2 else 10) <= len(data):
        if version == 2:
            frame_id = data[pos:pos + 3].decode('latin-1')
            frame_size = int.from_bytes(data[pos + 3:pos + 6], 'big')
            pos += 6
        else:
            frame_id = data[pos:pos + 4].decode('latin-1')
            raw_size = data[pos + 4:pos + 8]
            frame_size = _syncsafe(raw_size) if version == 4 else int.from_bytes(
                raw_size, 'big')
            frame_flags = int.from_bytes(data[pos + 8:pos + 10], 'big')
            pos += 10

        if not frame_id.strip('\x00'):
            break
        if frame_size <= 0 or pos + frame_size > len(data):
            break

        payload = data[pos:pos + frame_size]
        pos += frame_size

        if version == 4:
            if frame_flags & 0x000C:       # compressed or encrypted
                continue
            if frame_flags & 0x0040:       # grouping identity
                payload = payload[1:]
            if frame_flags & 0x0001:       # data length indicator
                payload = payload[4:]
            if frame_flags & 0x0002:       # per-frame unsynchronisation
                payload = _unsync(payload)
        elif version == 3 and frame_flags & 0x00C0:
            continue

        frame_id = _V22_IDS.get(frame_id, frame_id)
        if frame_id in _WANTED and frame_id not in frames:
            frames[frame_id] = _decode_text(payload)

    footer = 10 if version == 4 and flags & 0x10 else 0
    return frames, 10 + size + footer


def _from_frames(frames: dict[str, str]) -> TrackTags:
    return TrackTags(
        title=frames.get('TIT2', ''),
        artist=frames.get('TPE1', ''),
        album=frames.get('TALB', ''),
        genre=normalize_genre(frames.get('TCON', '')),
        year=frames.get('TDRC', '') or frames.get('TYER', ''),
        track=frames.get('TRCK', ''),
    )


def _read_id3v1(path: str) -> TrackTags | None:
    try:
        size = os.path.getsize(path)
        if size < 128:
            return None
        with open(path, 'rb') as f:
            f.seek(-128, 2)
            data = f.read(128)
    except OSError:
        return None
    if data[:3] != b'TAG':
        return None

    def text(start: int, length: int) -> str:
        return data[start:start + length].split(b'\x00', 1)[0].decode(
            'latin-1', 'replace').strip(' \x00')

    genre_index = data[127]
    genre = (ID3V1_GENRES[genre_index]
             if genre_index < len(ID3V1_GENRES) else str(genre_index))
    return TrackTags(
        title=text(3, 30), artist=text(33, 30), album=text(63, 30),
        year=text(93, 4), genre=genre)


def read_tags(path: str) -> TrackTags | None:
    """Read a file's tags, or None if it has none or cannot be read.

    ID3v2 wins over ID3v1 when both are present.
    """
    frames, _offset = _read_id3v2(path)
    if frames:
        tags = _from_frames(frames)
        if not tags.empty:
            return tags

    tags = _read_id3v1(path)
    if tags is not None and not tags.empty:
        return tags
    return None


def _parse_mpeg_header(data: bytes, pos: int) -> dict[str, int] | None:
    if pos + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[pos:pos + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != 1:
        return None
    bitrate_index = (b2 >> 4) & 0x0F
    sample_index = (b2 >> 2) & 0x03
    if bitrate_index in (0, 15) or sample_index == 3:
        return None
    bitrate = _BITRATES[1 if version == 3 else 2][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version][sample_index]
    padding = (b2 >> 1) & 0x01
    channel_mode = b3 >> 6
    frame_len = (_FRAME_COEFF[version] * bitrate // sample_rate) + padding
    return {
        'version': version,
        'bitrate': bitrate,
        'sample_rate': sample_rate,
        'frame_len': frame_len,
        'samples': _SAMPLES_PER_FRAME[version],
        'channels': 1 if channel_mode == 3 else 2,
    }


def _xing_frames(data: bytes, pos: int, header: dict[str, int]) -> int | None:
    if header['version'] == 3:
        side = 17 if header['channels'] == 1 else 32
    else:
        side = 9 if header['channels'] == 1 else 17
    off = pos + 4 + side
    if data[off:off + 4] not in (b'Xing', b'Info'):
        return None
    flags = int.from_bytes(data[off + 4:off + 8], 'big')
    if not flags & 1:
        return None
    frames = int.from_bytes(data[off + 8:off + 12], 'big')
    return frames or None


def read_duration(path: str) -> float | None:
    """Approximate duration in seconds from the MP3 frame headers."""
    try:
        size = os.path.getsize(path)
        _frames, audio_start = _read_id3v2(path)
        with open(path, 'rb') as f:
            audio_end = size
            f.seek(-128, 2) if size >= 128 else None
            if size >= 128 and f.read(3) == b'TAG':
                audio_end -= 128
            f.seek(audio_start)
            probe = f.read(4096)
    except OSError:
        return None

    header = None
    pos = 0
    for pos in range(max(0, len(probe) - 3)):
        header = _parse_mpeg_header(probe, pos)
        if header is not None:
            break
    if header is None:
        return None

    xing = _xing_frames(probe, pos, header)
    if xing is not None:
        duration = xing * header['samples'] / header['sample_rate']
    else:
        audio_bytes = audio_end - (audio_start + pos)
        if audio_bytes <= 0:
            return None
        duration = audio_bytes * 8 / header['bitrate']

    return duration if 0 < duration < 86400 else None


def probe(path: str) -> tuple[TrackTags | None, float | None]:
    """Tags and duration for a file, either of which may be None."""
    return read_tags(path), read_duration(path)
