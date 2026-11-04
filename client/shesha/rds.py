"""RDS display helpers.

The decoder itself runs in firmware (firmware/src/rds_rx.c); this module only
turns its numeric state into something readable. The PTY tables differ
between the European RDS standard and the North American RBDS one, so both
are carried and the caller picks.

It also maps a track's ID3 tags onto the transmit fields: the genre to a PTY
code, and artist/title to the 64-character RadioText.
"""
from __future__ import annotations

from .mp3 import TrackTags, normalize_genre

PTY_NAMES_RDS = [
    'None', 'News', 'Current affairs', 'Information', 'Sport', 'Education',
    'Drama', 'Culture', 'Science', 'Varied', 'Pop music', 'Rock music',
    'Easy listening', 'Light classical', 'Serious classical', 'Other music',
    'Weather', 'Finance', "Children's", 'Social affairs', 'Religion',
    'Phone-in', 'Travel', 'Leisure', 'Jazz', 'Country', 'National',
    'Oldies', 'Folk', 'Documentary', 'Alarm test', 'Alarm',
]

PTY_NAMES_RBDS = [
    'None', 'News', 'Information', 'Sports', 'Talk', 'Rock', 'Classic rock',
    'Adult hits', 'Soft rock', 'Top 40', 'Country', 'Oldies', 'Soft music',
    'Nostalgia', 'Jazz', 'Classical', 'Rhythm and blues', 'Soft R&B',
    'Language', 'Religious music', 'Religious talk', 'Personality',
    'Public', 'College', 'Spanish talk', 'Spanish music', 'Hip hop',
    'Unassigned', 'Folk', 'Weather', 'Emergency test', 'Emergency',
]


def pty_name(code: int, *, rbds: bool = True) -> str:
    """Program-type name for a 5-bit PTY code. RBDS is the US table."""
    table = PTY_NAMES_RBDS if rbds else PTY_NAMES_RDS
    if 0 <= code < len(table):
        return table[code]
    return 'Unknown'


def pty_text(code: int) -> str:
    """'10 Country'-style label, or '' when the station sends PTY 0.

    PTY 0 means "no programme type", not a type called "None"; showing the
    table's placeholder name for it reads as a decode failure.
    """
    if code == 0:
        return ''
    return f'{code} {pty_name(code)}'


def ps_text(ps: str, mask: int) -> str:
    """8-character PS for display.

    Characters not yet received render as ``_``. Stations routinely centre
    their name with spaces inside the fixed 8-character field, so the
    surrounding padding is trimmed; only ASCII spaces go, because ``_`` is
    load-bearing for the incomplete state.
    """
    text = ''.join(ch if (mask >> i) & 1 else '_'
                   for i, ch in enumerate(ps))
    return text.strip(' ')


def ct_text(state) -> str:
    """Local clock time from a 4A group, or '' if never received."""
    if not state.ct_valid:
        return ''
    minutes = state.ct_minutes
    sign = '-' if state.ct_offset_half_hours < 0 else '+'
    off = abs(state.ct_offset_half_hours)
    return (f'{minutes // 60:02d}:{minutes % 60:02d} '
            f'UTC{sign}{off // 2:02d}:{(off % 2) * 30:02d}')


# Canonical ID3v1 genre -> RBDS PTY code.  Every entry in the ID3v1 genre
# table has an explicit decision: either a code or None for genres with no
# sensible home in the 32-code RBDS table.  The coverage test keeps this in
# step with client/shesha/mp3.py.
_CANONICAL_BY_PTY = {
    4: ('Comedy', 'Humour', 'Pranks', 'Satire', 'Speech'),
    5: ('Acid Punk', 'AlternRock', 'Alternative', 'Black Metal', 'BritPop',
        'Crossover', 'Darkwave', 'Death Metal', 'Folk-Rock', 'Gothic',
        'Gothic Rock', 'Grunge', 'Hard Rock', 'Hardcore', 'Heavy Metal',
        'Indie', 'Industrial', 'Metal', 'Negerpunk', 'New Wave',
        'Polsk Punk', 'Power Ballad', 'Primus', 'Progressive Rock',
        'Psychedelic Rock', 'Psychadelic', 'Punk', 'Punk Rock', 'Rock',
        'Rock & Roll', 'Ska', 'Slow Rock', 'Southern Rock', 'Symphonic Rock',
        'Techno-Industrial', 'Thrash Metal'),
    6: ('Classic Rock',),
    9: ('Acid', 'Anime', 'Beat', 'Club', 'Club-House', 'Dance', 'Dance Hall',
        'Disco', 'Drum & Bass', 'Electronic', 'Euro-House', 'Euro-Techno',
        'Eurodance', 'Freestyle', 'Goa', 'House', 'JPop', 'Jungle', 'Pop',
        'Pop/Funk', 'Rave', 'Synthpop', 'Techno', 'Top 40', 'Trance'),
    10: ('Bluegrass', 'Country'),
    11: ('Oldies', 'Retro'),
    12: ('A capella', 'Acoustic', 'Ambient', 'Ballad', 'Cabaret', 'Chanson',
         'Chorus', 'Dream', 'Duet', 'Easy Listening', 'Lo-Fi', 'Meditative',
         'Musical', 'New Age', 'Showtunes', 'Space', 'Vocal'),
    14: ('Acid Jazz', 'Bebob', 'Big Band', 'Fast Fusion', 'Fusion', 'Jazz',
         'Jazz+Funk', 'Swing'),
    15: ('Avantgarde', 'Chamber Music', 'Classical', 'Drum Solo',
         'Instrumental', 'Instrumental Pop', 'Instrumental Rock', 'Opera',
         'Sonata', 'Soundtrack', 'Symphony', 'Trailer'),
    16: ('Bass', 'Blues', 'Funk', 'R&B', 'Rhythmic Soul', 'Slow Jam', 'Soul'),
    19: ('Christian Rock', 'Contemporary Christian', 'Gospel', 'Revival'),
    25: ('Latin', 'Merengue', 'Salsa', 'Samba', 'Tango'),
    26: ('Booty Bass', 'Christian Gangsta Rap', 'Christian Rap', 'Gangsta',
         'Hip-Hop', 'Rap', 'Trip-Hop'),
    28: ('Celtic', 'Ethnic', 'Folk', 'Folklore', 'National Folk',
         'Native American', 'Polka', 'Pop-Folk', 'Tribal'),
}

_UNMAPPED = (
    'Cult', 'Game', 'Noise', 'Other', 'Porn Groove', 'Reggae', 'Sound Clip',
    'Terror',
)

CANONICAL_PTY: dict[str, int | None] = {
    name.lower(): code
    for code, names in _CANONICAL_BY_PTY.items()
    for name in names
}
CANONICAL_PTY.update({name.lower(): None for name in _UNMAPPED})

# Free-text ID3v2 genres are arbitrary.  These aliases are the common
# spelling and sub-genre variants that do not appear in the ID3v1 table.
_ALIASES = {
    'adult contemporary': 7,
    'adult hits': 7,
    'alt rock': 5,
    'alternative rock': 5,
    'americana': 10,
    'audiobook': 4,
    'baroque': 15,
    'bossa nova': 25,
    'chillout': 12,
    'chiptune': 9,
    'city pop': 9,
    'contemporary r&b': 16,
    'country rock': 10,
    'dance pop': 9,
    'djent': 5,
    'doom metal': 5,
    'downtempo': 12,
    'edm': 9,
    'electronica': 9,
    'emo': 5,
    'emergency': 31,
    'folk rock': 5,
    'foreign language': 18,
    'garage rock': 5,
    'grime': 26,
    'hip hop': 26,
    'indie pop': 9,
    'indie rock': 5,
    'information': 2,
    'k-pop': 9,
    'latin pop': 25,
    'lounge': 12,
    'metalcore': 5,
    'motown': 16,
    'neo soul': 16,
    'new jack swing': 16,
    'news': 1,
    'nu metal': 5,
    'podcast': 4,
    'post-punk': 5,
    'post-rock': 5,
    'power metal': 5,
    'progressive metal': 5,
    'psychedelic': 5,
    'quiet storm': 17,
    'reggaeton': 25,
    'religious': 19,
    'religious music': 19,
    'religious talk': 20,
    'rhythm and blues': 16,
    'rock and roll': 5,
    'score': 15,
    'sermon': 20,
    'soft r&b': 17,
    'soft rock': 8,
    'sport': 3,
    'sports': 3,
    'spoken word': 4,
    'synth-pop': 9,
    'synthwave': 9,
    'talk': 4,
    'talk radio': 4,
    'trap': 26,
    'vaporwave': 9,
    'weather': 29,
    'world': 28,
    'world music': 28,
}


def genre_to_pty(genre: str) -> int | None:
    """RBDS PTY code for an ID3 genre, or None if it maps to nothing.

    Accepts a canonical name ('Rock'), a free-text variant ('Hip Hop'), or
    an ID3v1 index ('17' / '(17)').
    """
    name = normalize_genre(genre)
    if not name:
        return None
    key = name.strip().lower()
    if key in CANONICAL_PTY:
        return CANONICAL_PTY[key]
    return _ALIASES.get(key)


def rds_from_tags(tags: TrackTags) -> tuple[str | None, int | None]:
    """Map tags to (RadioText, PTY) for transmission.

    RadioText is the conventional ``Artist - Title``; either alone is used
    when only one is tagged.  Fields that cannot be derived are None, so the
    caller can keep a manual override.
    """
    artist = tags.artist.strip()
    title = tags.title.strip()
    if artist and title:
        rt: str | None = f'{artist} - {title}'
    else:
        rt = title or artist or None
    if rt is not None:
        rt = rt[:64]
    return (rt, genre_to_pty(tags.genre))

