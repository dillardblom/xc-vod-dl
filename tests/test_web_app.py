from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xc_vod_dl.config import AccountConfig, Config, DownloadConfig
from xc_vod_dl.web.app import create_app

pytestmark = pytest.mark.integration


def _client(base_url: str, tmp_path: Path) -> TestClient:
    config = Config(
        account=AccountConfig(server=base_url, username="demo", password="demo"),
        download=DownloadConfig(
            movies_dir=tmp_path / "Movies",
            series_dir=tmp_path / "Series",
            state_db=tmp_path / "state.db",
            download_nfo=False,
            download_cover=False,
        ),
    )
    return TestClient(create_app(config))


def test_index_serves_html(xtream_server, tmp_path):
    base_url, _state = xtream_server
    client = _client(base_url, tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "xc-vod-dl" in res.text


def test_search_movies_matches_by_substring(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["vod_categories"] = [{"category_id": "1", "category_name": "Action"}]
    state["vod_streams"] = [
        {"stream_id": 101, "name": "Example Movie", "category_id": "1", "container_extension": "mp4"},
        {"stream_id": 102, "name": "Something Else", "category_id": "1", "container_extension": "mp4"},
    ]
    client = _client(base_url, tmp_path)

    res = client.get("/api/movies/search", params={"q": "example"})

    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0] == {
        "id": 101,
        "name": "Example Movie",
        "category": "Action",
        "container_extension": "mp4",
        "year": None,
        "duration": None,
    }


def test_search_movies_includes_year_when_present(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["vod_categories"] = [{"category_id": "1", "category_name": "Action"}]
    state["vod_streams"] = [
        {
            "stream_id": 101,
            "name": "Old Movie",
            "category_id": "1",
            "container_extension": "mp4",
            "year": 1999,
        },
    ]
    client = _client(base_url, tmp_path)

    res = client.get("/api/movies/search", params={"q": "old"})

    assert res.json()[0]["year"] == "1999"


def test_search_movies_includes_duration_when_present(xtream_server, tmp_path):
    """Confirmed against real servers: get_vod_info()'s duration is
    sometimes populated for movies (unlike video/audio, which never are) —
    fetched per result the same way series search already pays for
    season/episode counts."""
    base_url, state = xtream_server
    state["vod_categories"] = [{"category_id": "1", "category_name": "Action"}]
    state["vod_streams"] = [
        {"stream_id": 101, "name": "Long Movie", "category_id": "1", "container_extension": "mkv"},
    ]
    state["vod_info"]["101"] = {
        "info": {"name": "Long Movie", "duration": "01:51:00"},
        "movie_data": {"stream_id": 101, "name": "Long Movie", "container_extension": "mkv"},
    }
    client = _client(base_url, tmp_path)

    res = client.get("/api/movies/search", params={"q": "long"})

    assert res.json()[0]["duration"] == "01:51:00"


def test_search_movies_empty_query_returns_nothing(xtream_server, tmp_path):
    base_url, _state = xtream_server
    client = _client(base_url, tmp_path)
    res = client.get("/api/movies/search", params={"q": ""})
    assert res.json() == []


def test_search_series_reports_gaps(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["series_categories"] = [{"category_id": "3", "category_name": "Sci-Fi"}]
    state["series_streams"] = [
        {"series_id": 6789, "name": "Gappy Show", "category_id": "3"}
    ]
    state["series_info"]["6789"] = {
        "seasons": [],
        "info": {"name": "Gappy Show"},
        "episodes": {
            "1": [
                {"id": "1", "episode_num": 1, "title": "E1", "container_extension": "mkv", "season": 1, "info": {}},
                {"id": "3", "episode_num": 3, "title": "E3", "container_extension": "mkv", "season": 1, "info": {}},
            ]
        },
    }
    client = _client(base_url, tmp_path)

    res = client.get("/api/series/search", params={"q": "gappy"})

    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["seasons"] == [1]
    assert data[0]["episode_count"] == 2
    assert data[0]["resolution"] is None  # not populated in this fixture
    assert data[0]["gaps"] == {"1": [2]}
    assert "S01E02" in data[0]["report"]


def test_search_series_includes_sample_resolution_when_present(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["series_categories"] = [{"category_id": "3", "category_name": "Sci-Fi"}]
    state["series_streams"] = [{"series_id": 6789, "name": "HD Show", "category_id": "3"}]
    state["series_info"]["6789"] = {
        "seasons": [],
        "info": {"name": "HD Show"},
        "episodes": {
            "1": [
                {
                    "id": "1",
                    "episode_num": 1,
                    "title": "E1",
                    "container_extension": "mkv",
                    "season": 1,
                    "info": {"video": {"width": 1920, "height": 1080}},
                },
            ]
        },
    }
    client = _client(base_url, tmp_path)

    res = client.get("/api/series/search", params={"q": "hd"})

    assert res.json()[0]["resolution"] == "1920x1080"


def test_queue_download_and_job_reaches_done(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["vod_info"]["101"] = {
        "info": {"name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "Example Movie", "container_extension": "mp4"},
    }
    client = _client(base_url, tmp_path)

    res = client.post("/api/download", json=[{"kind": "movie", "id": 101}])
    assert res.status_code == 202
    assert res.json() == {"queued": 1}

    import time

    deadline = time.time() + 10
    jobs = {}
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        if jobs and all(j["status"] in ("done", "failed") for j in jobs.values()):
            break
        time.sleep(0.1)

    assert jobs, "no job appeared within the deadline"
    (job,) = jobs.values()
    assert job["status"] == "done"
    assert (tmp_path / "Movies" / "Example Movie (2024)" / "Example Movie (2024).mp4").exists()


def test_queue_download_with_rename(xtream_server, tmp_path):
    base_url, state = xtream_server
    state["vod_info"]["101"] = {
        "info": {"name": "NL Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "NL Example Movie", "container_extension": "mp4"},
    }
    client = _client(base_url, tmp_path)

    res = client.post(
        "/api/download", json=[{"kind": "movie", "id": 101, "display_name": "Example Movie"}]
    )
    assert res.status_code == 202

    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        if jobs and all(j["status"] in ("done", "failed") for j in jobs.values()):
            break
        time.sleep(0.1)

    assert (tmp_path / "Movies" / "Example Movie (2024)" / "Example Movie (2024).mp4").exists()
