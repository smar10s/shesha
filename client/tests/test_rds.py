"""Tests for the RDS display helpers (PTY names, clock time)."""
from types import SimpleNamespace

from shesha.rds import (
    PTY_NAMES_RDS, PTY_NAMES_RBDS, pty_name, pty_text, ps_text, ct_text,
    genre_to_pty, rds_from_tags,
)
from shesha.mp3 import TrackTags


def test_pty_tables_are_32_entries():
    assert len(PTY_NAMES_RDS) == 32
    assert len(PTY_NAMES_RBDS) == 32


def test_pty_names_differ_between_standards():
    # The famous mismatch: RBDS 3 is Sports, RDS 3 is Information.
    assert pty_name(3, rbds=True) == 'Sports'
    assert pty_name(3, rbds=False) == 'Information'


def test_pty_name_known_codes():
    assert pty_name(0) == 'None'
    assert pty_name(10) == 'Country'
    assert pty_name(31) == 'Emergency'


def test_pty_name_out_of_range():
    assert pty_name(32) == 'Unknown'
    assert pty_name(-1) == 'Unknown'


def test_pty_text_zero_is_blank():
    """PTY 0 is 'undefined'; the display layer must not say 'None'."""
    assert pty_text(0) == ''
    assert pty_text(10) == '10 Country'


def test_ps_text_trims_station_padding():
    """Stations centre PS with spaces; the name is what matters."""
    assert ps_text('  NWPB  ', 0xFF) == 'NWPB'
    assert ps_text('SHESHA  ', 0xFF) == 'SHESHA'


def test_ps_text_marks_unreceived_characters():
    assert ps_text('  NWPB  ', 0x07) == 'N_____'
    assert ps_text('NWPB    ', 0xF0) == '____'


def test_ct_text_positive_offset():
    s = SimpleNamespace(ct_valid=True, ct_minutes=12 * 60 + 34,
                        ct_offset_half_hours=3)
    assert ct_text(s) == '12:34 UTC+01:30'


def test_ct_text_negative_offset():
    s = SimpleNamespace(ct_valid=True, ct_minutes=5,
                        ct_offset_half_hours=-10)
    assert ct_text(s) == '00:05 UTC-05:00'


def test_ct_text_invalid():
    s = SimpleNamespace(ct_valid=False, ct_minutes=0,
                        ct_offset_half_hours=0)
    assert ct_text(s) == ''


# ─── ID3 tags -> RDS transmit fields ─────────────────────────────────


def test_rds_from_tags_artist_and_title():
    tags = TrackTags(title='Sunlight', artist='The Band')
    rt, pty = rds_from_tags(tags)
    assert rt == 'The Band - Sunlight'
    assert pty is None


def test_rds_from_tags_title_only():
    assert rds_from_tags(TrackTags(title='Sunlight')) == ('Sunlight', None)


def test_rds_from_tags_artist_only():
    assert rds_from_tags(TrackTags(artist='The Band')) == ('The Band', None)


def test_rds_from_tags_empty():
    assert rds_from_tags(TrackTags()) == (None, None)


def test_rds_from_tags_truncates_radiotext():
    rt, _ = rds_from_tags(TrackTags(artist='A' * 40, title='T' * 40))
    assert rt == ('A' * 40 + ' - ' + 'T' * 40)[:64]
    assert len(rt) == 64


def test_rds_from_tags_maps_genre_to_pty():
    # RBDS code assignments (the client's display default).
    assert rds_from_tags(TrackTags(title='x', genre='Rock'))[1] == 5
    assert rds_from_tags(TrackTags(title='x', genre='Jazz'))[1] == 14
    assert rds_from_tags(TrackTags(title='x', genre='Country'))[1] == 10
    assert rds_from_tags(TrackTags(title='x', genre='Classical'))[1] == 15
    assert rds_from_tags(TrackTags(title='x', genre='Hip-Hop'))[1] == 26


def test_rds_from_tags_genre_is_case_insensitive():
    assert rds_from_tags(TrackTags(title='x', genre='rock'))[1] == 5


def test_rds_from_tags_unknown_genre_has_no_pty():
    assert rds_from_tags(TrackTags(title='x', genre='Bebop Fusion'))[1] is None


def test_genre_to_pty_numeric_index():
    """ID3v1 genres arrive as indices; resolve them to the standard name."""
    assert genre_to_pty('17') == 5          # Rock
    assert genre_to_pty('(17)') == 5
    assert genre_to_pty('8') == 14          # Jazz


def test_genre_to_pty_blank_and_unknown():
    assert genre_to_pty('') is None
    assert genre_to_pty('   ') is None
    assert genre_to_pty('Underwater Basketry') is None
    assert genre_to_pty('999') is None



def test_rds_from_tags_metal_genres_map_to_rock():
    assert rds_from_tags(TrackTags(title='x', genre='Heavy Metal'))[1] == 5
    assert rds_from_tags(TrackTags(title='x', genre='Black Metal'))[1] == 5
    assert rds_from_tags(TrackTags(title='x', genre='Thrash Metal'))[1] == 5
    assert rds_from_tags(TrackTags(title='x', genre='(137)'))[1] == 5


def test_every_id3v1_genre_has_an_explicit_pty_decision():
    from shesha.mp3 import ID3V1_GENRES
    from shesha.rds import CANONICAL_PTY

    assert set(g.lower() for g in ID3V1_GENRES) == set(CANONICAL_PTY)
    assert all(code is None or 0 <= code <= 31
               for code in CANONICAL_PTY.values())


def test_free_text_genre_aliases():
    assert genre_to_pty('Hip Hop') == 26
    assert genre_to_pty('EDM') == 9
    assert genre_to_pty('Soft Rock') == 8
    assert genre_to_pty('Adult Contemporary') == 7
    assert genre_to_pty('Power Metal') == 5
    assert genre_to_pty('Reggaeton') == 25
    assert genre_to_pty('Podcast') == 4
    assert genre_to_pty('Underwater Basketry') is None
