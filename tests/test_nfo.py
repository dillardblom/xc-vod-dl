from xc_vod_dl.api.models import Episode, SeriesInfo, VodInfo
from xc_vod_dl.nfo import build_episode_nfo, build_movie_nfo, build_series_nfo


def test_build_movie_nfo_includes_core_fields():
    vod = VodInfo(
        name="Example Movie",
        plot="A test plot.",
        genre="Drama",
        release_date="2024-03-15",
        tmdb_id="550",
        stream_id=101,
        container_extension="mkv",
    )
    xml = build_movie_nfo(vod)
    assert "<movie>" in xml
    assert "<title>Example Movie</title>" in xml
    assert "<plot>A test plot.</plot>" in xml
    assert '<uniqueid type="tmdb" default="true">550</uniqueid>' in xml


def test_build_movie_nfo_omits_uniqueid_when_no_tmdb_id():
    vod = VodInfo(
        name="No TMDB Movie",
        plot="",
        genre="",
        release_date="",
        tmdb_id=None,
        stream_id=102,
        container_extension="mkv",
    )
    xml = build_movie_nfo(vod)
    assert "uniqueid" not in xml


def test_build_episode_nfo_includes_season_and_episode():
    episode = Episode(
        episode_id=9001,
        season=1,
        episode_num=1,
        title="Pilot",
        container_extension="mkv",
        plot="First episode.",
        tmdb_id="12345",
    )
    xml = build_episode_nfo(episode, series_name="Example Series")
    assert "<title>Pilot</title>" in xml
    assert "<showtitle>Example Series</showtitle>" in xml
    assert "<season>1</season>" in xml
    assert "<episode>1</episode>" in xml


def test_build_series_nfo_includes_core_fields():
    series = SeriesInfo(
        name="Example Series",
        plot="A series plot.",
        genre="Sci-Fi",
        tmdb_id="12345",
        seasons=[],
    )
    xml = build_series_nfo(series)
    assert "<tvshow>" in xml
    assert "<title>Example Series</title>" in xml
    assert "<genre>Sci-Fi</genre>" in xml


def test_nfo_escapes_special_characters():
    vod = VodInfo(
        name="Tom & Jerry: <Trouble>",
        plot="",
        genre="",
        release_date="",
        tmdb_id=None,
        stream_id=103,
        container_extension="mkv",
    )
    xml = build_movie_nfo(vod)
    assert "Tom &amp; Jerry" in xml
    assert "&lt;Trouble&gt;" in xml
