import shutil
from pathlib import Path

import pytest
import requests

from xc_vod_dl.download.engine import DownloadEngine, DownloadJob, download_file, tmp_path_for
from xc_vod_dl.exceptions import DownloadError
from xc_vod_dl.state.store import StateStore

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.integration


def test_download_file_fresh(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    n = download_file(requests.Session(), url, target)
    assert n == len(state["data"])
    assert target.read_bytes() == state["data"]
    assert state["requests"] == [None]


def test_download_file_resumes_from_existing_partial(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    half = len(state["data"]) // 2
    target.write_bytes(state["data"][:half])

    n = download_file(requests.Session(), url, target)

    assert n == len(state["data"])
    assert target.read_bytes() == state["data"]
    assert state["requests"] == [f"bytes={half}-"]


def test_download_file_discards_partial_when_range_ignored(media_server, tmp_path):
    url, state = media_server
    state["mode"] = "ignore_range"
    target = tmp_path / "out.mp4"
    target.write_bytes(b"garbage-partial-that-does-not-match-source")

    n = download_file(requests.Session(), url, target)

    assert n == len(state["data"])
    assert target.read_bytes() == state["data"]


def test_download_file_raises_on_server_error(media_server, tmp_path):
    url, state = media_server
    state["mode"] = "fail_503"
    target = tmp_path / "out.mp4"
    with pytest.raises(DownloadError):
        download_file(requests.Session(), url, target)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_downloads_verifies_and_commits(media_server, tmp_path):
    url, _state = media_server
    target = tmp_path / "Movies" / "Example (2024)" / "Example (2024).mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")

    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store, verify_mode="quick")
        ok = engine.run(job)

        assert ok is True
        assert target.exists()
        assert not tmp_path_for(target).exists()
        record = state_store.get("movie:1")
        assert record.status == "done"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_recovers_from_mid_stream_drop(media_server, tmp_path):
    """The scenario this whole project is built around: a hiccup mid-download,
    followed by an automatic resume that finishes cleanly and verifies."""
    url, state = media_server
    state["mode"] = "flaky_once"
    target = tmp_path / "Movies" / "Example (2024)" / "Example (2024).mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")

    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(
            requests.Session(), state_store, verify_mode="quick", max_attempts=3, chunk_size=512
        )
        ok = engine.run(job)

        assert ok is True
        assert target.read_bytes() == state["data"]
        # First request got cut off with no Range header, second resumed from
        # partway through — proves the retry actually resumed, not restarted.
        assert state["requests"][0] is None
        assert state["requests"][1] is not None


def test_engine_run_skips_when_target_already_exists(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    target.write_bytes(b"already here")

    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store)
        job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")
        ok = engine.run(job)

        assert ok is True
        assert state["requests"] == []  # never touched the network
        assert state_store.get("movie:1").status == "done"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_fails_after_exhausting_retries_on_bad_source(media_server, tmp_path):
    url, state = media_server
    state["data"] = b"not a real media file at all"
    target = tmp_path / "out.mp4"

    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store, verify_mode="quick", max_attempts=2)
        job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")
        ok = engine.run(job)

        assert ok is False
        assert not target.exists()
        assert not tmp_path_for(target).exists()
        assert state_store.get("movie:1").status == "failed"
