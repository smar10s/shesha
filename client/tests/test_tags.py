"""Tests for the MP3 tag and duration reader."""
import pytest

from shesha.mp3 import TrackTags, probe, read_duration, read_tags

from tests.mp3_fixtures import id3v1, write_mp3, write_tagged


def test_read_tags_id3v23(tmp_path):
    p = tmp_path / 'song.mp3'
    write_tagged(p, {'title': 'Sunlight', 'artist': 'The Band',
                     'album': 'Album X', 'genre': 'Rock', 'year': '1999',
                     'track': '3/12'}, version=3)
    assert read_tags(str(p)) == TrackTags(
        title='Sunlight', artist='The Band', album='Album X', genre='Rock',
        year='1999', track='3/12')


def test_read_tags_id3v24(tmp_path):
    p = tmp_path / 'song.mp3'
    write_tagged(p, {'title': 'Sunlight', 'artist': 'The Band'}, version=4)
    tags = read_tags(str(p))
    assert tags.title == 'Sunlight'
    assert tags.artist == 'The Band'


def test_read_tags_id3v24_syncsafe_frame_size(tmp_path):
    p = tmp_path / 'long.mp3'
    title = 'x' * 200
    write_tagged(p, {'title': title}, version=4)
    assert read_tags(str(p)).title == title


def test_read_tags_id3v22_short_frame_ids(tmp_path):
    p = tmp_path / 'old.mp3'
    write_tagged(p, {'title': 'Old Title', 'artist': 'Old Artist'},
                 version=2)
    tags = read_tags(str(p))
    assert tags.title == 'Old Title'
    assert tags.artist == 'Old Artist'


def test_read_tags_id3v2_utf16(tmp_path):
    """Encoding=1 (UTF-16 with BOM) must survive the round trip."""
    p = tmp_path / 'unicode.mp3'
    write_tagged(p, {'title': 'Caf\u00e9 \u2014 \u2603'}, encoding=1)
    assert read_tags(str(p)).title == 'Caf\u00e9 \u2014 \u2603'


def test_read_tags_id3v1_fallback(tmp_path):
    p = tmp_path / 'old.mp3'
    write_mp3(p, trailer=id3v1(title='Old Title', artist='Old Artist',
                               album='Old Album', year='1980', genre=17))
    tags = read_tags(str(p))
    assert tags.title == 'Old Title'
    assert tags.artist == 'Old Artist'
    assert tags.album == 'Old Album'
    assert tags.year == '1980'
    assert tags.genre == 'Rock'


def test_read_tags_prefers_v2_over_v1(tmp_path):
    p = tmp_path / 'both.mp3'
    write_mp3(p, {'title': 'New Title', 'artist': 'New Artist'},
              trailer=id3v1(title='Old Title', artist='Old Artist', genre=17))
    tags = read_tags(str(p))
    assert tags.title == 'New Title'
    assert tags.artist == 'New Artist'
    assert tags.genre == ''


def test_read_tags_returns_none_without_tags(tmp_path):
    p = tmp_path / 'plain.bin'
    p.write_bytes(b'not audio and not tagged')
    assert read_tags(str(p)) is None


def test_read_tags_missing_file(tmp_path):
    assert read_tags(str(tmp_path / 'nope.mp3')) is None


def test_track_tags_empty():
    assert TrackTags().empty
    assert not TrackTags(title='x').empty


def test_read_duration_cbr(tmp_path):
    p = tmp_path / 'cbr.mp3'
    write_mp3(p, frames=100)
    assert read_duration(str(p)) == pytest.approx(2.606, abs=1e-3)


def test_read_duration_uses_xing_frame_count(tmp_path):
    p = tmp_path / 'vbr.mp3'
    write_mp3(p, frames=5, xing_frames=1000)
    assert read_duration(str(p)) == pytest.approx(1000 * 1152 / 44100)


def test_read_duration_skips_id3_tags(tmp_path):
    p = tmp_path / 'tagged.mp3'
    write_mp3(p, {'title': 'x', 'artist': 'y'}, frames=100,
              trailer=id3v1(title='x'))
    assert read_duration(str(p)) == pytest.approx(2.606, abs=1e-3)


def test_read_duration_rejects_non_mp3(tmp_path):
    p = tmp_path / 'plain.bin'
    p.write_bytes(b'not audio')
    assert read_duration(str(p)) is None


def test_probe_returns_tags_and_duration(tmp_path):
    p = tmp_path / 'both.mp3'
    write_mp3(p, {'title': 'Sunlight'}, frames=100)
    tags, duration = probe(str(p))
    assert tags.title == 'Sunlight'
    assert duration == pytest.approx(2.606, abs=1e-3)


def test_normalize_genre_parenthesised_refinement():
    from shesha.mp3 import normalize_genre

    assert normalize_genre('(17)Rock') == 'Rock'
    assert normalize_genre('(17)') == 'Rock'
    assert normalize_genre('(17)Hard Rock') == 'Hard Rock'
