from pathlib import Path

import pytest

from xc_vod_dl.jobs import JobSpec, parse_manifest, parse_manifest_line


def test_parse_movie_line():
    assert parse_manifest_line("movie:12345") == JobSpec(kind="movie", id=12345)


def test_parse_whole_series_line():
    assert parse_manifest_line("series:6789") == JobSpec(kind="series", id=6789)


def test_parse_series_season_line():
    assert parse_manifest_line("series:6789:2") == JobSpec(kind="series", id=6789, season=2)


def test_parse_series_episode_line():
    assert parse_manifest_line("series:6789:2:5") == JobSpec(
        kind="series", id=6789, season=2, episode=5
    )


def test_blank_and_comment_lines_are_ignored():
    assert parse_manifest_line("") is None
    assert parse_manifest_line("   ") is None
    assert parse_manifest_line("# a comment") is None


def test_inline_comment_is_stripped():
    assert parse_manifest_line("movie:12345  # my favorite movie") == JobSpec(kind="movie", id=12345)


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        parse_manifest_line("bogus:1")


def test_movie_with_extra_parts_raises():
    with pytest.raises(ValueError):
        parse_manifest_line("movie:1:2")


def test_non_integer_id_raises():
    with pytest.raises(ValueError):
        parse_manifest_line("movie:abc")


def test_parse_manifest_reads_multiple_lines(tmp_path: Path):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("movie:1\n# a comment\n\nseries:2\nseries:3:1\nseries:4:1:2")
    specs = parse_manifest(manifest)
    assert specs == [
        JobSpec(kind="movie", id=1),
        JobSpec(kind="series", id=2),
        JobSpec(kind="series", id=3, season=1),
        JobSpec(kind="series", id=4, season=1, episode=2),
    ]
