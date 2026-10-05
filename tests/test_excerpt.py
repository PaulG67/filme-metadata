import struct

from pathlib import Path
from types import SimpleNamespace

from app.excerpt import (
    ExcerptError,
    clock,
    content_words,
    movie_hash,
    parse_subtitle_hit,
    preview_offset,
    quote_match,
    sample_offsets,
    srt_to_text,
)
from app.listen import clip_file, clip_label


def test_hash_of_empty_edges_is_the_file_size():
    size = 131072
    head = bytes(65536)
    tail = bytes(65536)
    assert movie_hash(head, tail, size) == "0000000000020000"


def test_hash_changes_when_the_tail_changes():
    size = 200000
    head = bytes(65536)
    tail = bytearray(65536)
    first = movie_hash(head, bytes(tail), size)
    tail[10] = 7
    assert movie_hash(head, bytes(tail), size) != first


def test_hash_matches_chunk_sum():
    data = bytes(range(256)) * 800
    size = len(data)
    expected = size
    for chunk in (data[:65536], data[-65536:]):
        for (value,) in struct.iter_unpack("<q", chunk):
            expected = (expected + value) & 0xFFFFFFFFFFFFFFFF
    assert movie_hash(data[:65536], data[-65536:], size) == f"{expected:016x}"


def test_sample_offsets_skip_the_opening():
    offsets = sample_offsets(3600, 20)
    assert offsets[0] >= 60
    assert all(offset < 3600 - 20 for offset in offsets)


def test_short_file_starts_at_zero():
    assert sample_offsets(12, 20) == [0]


def test_quote_matches_the_spoken_line_and_not_the_other_show():
    transcript = "The royal family of Attilan has fallen to a military coup"
    marvel = srt_to_text(
        "1\n00:12:40,000 --> 00:12:44,000\nThe royal family of Attilan has fallen to a military coup\n"
    )
    hotel = srt_to_text(
        "1\n00:01:00,000 --> 00:01:04,000\nWelcome to the hotel. The concierge awaits the assassin.\n"
    )
    matched, phrase = quote_match(transcript, marvel)
    assert matched is True
    assert "attilan" in phrase
    assert quote_match(transcript, hotel) == (False, "")


def test_short_transcript_does_not_match():
    assert quote_match("ja und der", "ja und der royal family of attilan") == (False, "")


def test_srt_drops_timestamps_and_tags():
    text = srt_to_text("2\n00:00:01,000 --> 00:00:02,000\n<i>Hello</i> there\n")
    assert text == "Hello there"
    assert "hello" in " ".join(content_words(text))


def test_episode_hit_uses_the_series_identity():
    hit = parse_subtitle_hit(
        {
            "moviehash_match": True,
            "parent_imdb_id": 4154858,
            "parent_tmdb_id": 68716,
            "parent_title": "Marvel's Inhumans",
            "feature_details": {
                "feature_type": "Episode",
                "year": 2017,
                "title": "Behold... The Inhumans",
                "imdb_id": 7322094,
                "tmdb_id": 999,
            },
        }
    )
    assert hit is not None
    assert hit["kind"] == "series"
    assert hit["name"] == "Marvel's Inhumans"
    assert hit["imdb"] == "tt4154858"
    assert hit["tmdb"] == "68716"
    assert hit["year"] == 2017


def test_movie_hit_keeps_the_movie_id():
    hit = parse_subtitle_hit(
        {
            "moviehash_match": True,
            "feature_details": {
                "feature_type": "Movie",
                "year": 2021,
                "movie_name": "Dune",
                "imdb_id": 1160419,
                "tmdb_id": 438631,
            },
        }
    )
    assert hit["kind"] == "movie"
    assert hit["imdb"] == "tt1160419"
    assert hit["tmdb"] == "438631"


def test_clock():
    assert clock(754) == "12:34"
    assert clock(3661) == "1:01:01"


def test_preview_offset_uses_the_middle_of_the_film():
    assert preview_offset(7200, 20) == 3456
    assert preview_offset(10, 20) == 0


def test_clip_file_rejects_paths_outside_the_data_dir():
    settings = SimpleNamespace(data_dir=Path("data"))
    assert clip_file(settings, "../secrets") is None
    assert clip_file(settings, "abcd") is None
    path = clip_file(settings, "series-1-id")
    assert path == Path("data") / "clips" / "series-1-id.mp4"


def test_clip_label_names_the_episode():
    label = clip_label(
        {
            "Type": "Episode",
            "Name": "Behold... The Inhumans",
            "SeriesName": "Marvel's Inhumans",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
        },
        754,
    )
    assert label == "Marvel's Inhumans · S01E01 Behold... The Inhumans · ab 12:34"


def test_excerpt_error_can_carry_the_clip():
    assert ExcerptError("kein Dialog").clip is None
    err = ExcerptError("kein Dialog", clip={"clip_id": "series-1-id"})
    assert err.clip["clip_id"] == "series-1-id"
