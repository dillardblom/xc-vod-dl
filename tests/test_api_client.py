import urllib.parse

import responses

from xc_vod_dl.api.client import XtreamClient

SERVER = "https://xc.example.com"


def make_client() -> XtreamClient:
    return XtreamClient(SERVER, "demo", "demo")


@responses.activate
def test_get_account_parses_max_connections(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("account.json"), status=200
    )
    account = make_client().get_account()
    assert account.username == "demo"
    assert account.status == "Active"
    assert account.max_connections == 2
    assert account.active_cons == 0
    assert account.is_trial is False


@responses.activate
def test_get_vod_categories(load_fixture):
    responses.add(
        responses.GET,
        f"{SERVER}/player_api.php",
        json=load_fixture("vod_categories.json"),
        status=200,
    )
    cats = make_client().get_vod_categories()
    assert len(cats) == 2
    assert cats[0].category_id == "1"
    assert cats[0].category_name == "Action"


@responses.activate
def test_get_vod_streams(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("vod_streams.json"), status=200
    )
    streams = make_client().get_vod_streams(category_id="1")
    assert len(streams) == 1
    assert streams[0].stream_id == 101
    assert streams[0].container_extension == "mkv"


@responses.activate
def test_get_vod_streams_parses_year_when_present():
    responses.add(
        responses.GET,
        f"{SERVER}/player_api.php",
        json=[
            {
                "stream_id": 101,
                "name": "Example Movie",
                "category_id": "1",
                "container_extension": "mkv",
                "year": 1999,
            }
        ],
        status=200,
    )
    streams = make_client().get_vod_streams()
    assert streams[0].year == "1999"


@responses.activate
def test_get_vod_streams_year_is_none_when_absent(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("vod_streams.json"), status=200
    )
    streams = make_client().get_vod_streams()
    assert streams[0].year is None


@responses.activate
def test_get_vod_info(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("vod_info.json"), status=200
    )
    info = make_client().get_vod_info(101)
    assert info.name == "Example Movie (2024)"
    assert info.tmdb_id == "550"
    assert info.container_extension == "mkv"
    assert info.duration == "01:45:00"


@responses.activate
def test_get_vod_info_treats_zero_duration_as_absent():
    """Seen in the wild on a real server: the duration field present but
    "00:00:00" for one title in an otherwise fully populated catalog — not
    meaningfully different from the field being absent."""
    responses.add(
        responses.GET,
        f"{SERVER}/player_api.php",
        json={
            "info": {"name": "Example", "duration": "00:00:00"},
            "movie_data": {"stream_id": 101, "container_extension": "mp4"},
        },
        status=200,
    )
    info = make_client().get_vod_info(101)
    assert info.duration is None


@responses.activate
def test_get_series_streams(load_fixture):
    responses.add(
        responses.GET,
        f"{SERVER}/player_api.php",
        json=load_fixture("series_streams.json"),
        status=200,
    )
    series = make_client().get_series_streams()
    assert len(series) == 1
    # Regression guard: the real Xtream action for listing series is `get_series`,
    # not the more-guessable `get_series_streams` — confirmed against a real
    # Dispatcharr server, where sending the wrong action silently fell through
    # to the account-info handler instead of erroring, so this must be asserted
    # explicitly rather than trusted to surface via a response-shape mismatch.
    sent_query = urllib.parse.parse_qs(urllib.parse.urlparse(responses.calls[-1].request.url).query)
    assert sent_query["action"] == ["get_series"]
    assert series[0].series_id == 6789
    assert series[0].name == "Example Series"


@responses.activate
def test_get_series_info_builds_episodes_by_season(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("series_info.json"), status=200
    )
    info = make_client().get_series_info(6789)
    assert info.name == "Example Series"
    assert 1 in info.episodes
    season1 = info.episodes[1]
    assert len(season1) == 9  # 1-8, 10 — episode 9 deliberately missing in fixture
    assert [e.episode_num for e in season1][:3] == [1, 2, 3]
    assert season1[0].episode_id == 9001


@responses.activate
def test_get_series_info_parses_episode_resolution_when_present():
    responses.add(
        responses.GET,
        f"{SERVER}/player_api.php",
        json={
            "seasons": [],
            "info": {"name": "Example Series"},
            "episodes": {
                "1": [
                    {
                        "id": "9001",
                        "episode_num": 1,
                        "title": "Pilot",
                        "container_extension": "mkv",
                        "season": 1,
                        "info": {"video": {"width": 1920, "height": 1080}},
                    }
                ]
            },
        },
        status=200,
    )
    info = make_client().get_series_info(6789)
    assert info.episodes[1][0].resolution == "1920x1080"


@responses.activate
def test_get_series_info_resolution_is_none_when_absent(load_fixture):
    responses.add(
        responses.GET, f"{SERVER}/player_api.php", json=load_fixture("series_info.json"), status=200
    )
    info = make_client().get_series_info(6789)
    assert info.episodes[1][0].resolution is None


def test_movie_url_format():
    client = make_client()
    assert client.movie_url(101, "mkv") == f"{SERVER}/movie/demo/demo/101.mkv"


def test_episode_url_format():
    client = make_client()
    assert client.episode_url(9001, "mkv") == f"{SERVER}/series/demo/demo/9001.mkv"
