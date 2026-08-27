import shutil
from pathlib import Path
from unittest.mock import patch

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


def test_download_file_reports_total_from_content_length(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    totals = []
    download_file(requests.Session(), url, target, total_cb=totals.append)
    assert totals == [len(state["data"])]


def test_download_file_reports_total_accounting_for_resume_offset(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    half = len(state["data"]) // 2
    target.write_bytes(state["data"][:half])

    totals = []
    download_file(requests.Session(), url, target, total_cb=totals.append)

    # A 206 response's Content-Length is only the *remaining* bytes — the
    # reported total must still be the full file size, not just the remainder.
    assert totals == [len(state["data"])]


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


def test_download_file_treats_416_at_eof_as_already_complete(media_server, tmp_path):
    """A resume whose Range starts exactly at the resource's end — i.e. a
    prior attempt already wrote every byte to the .voddl file but never got
    to commit it — must be accepted as done, not fail. Real Xtream servers
    respond 416 to this rather than a 206 with zero remaining bytes."""
    url, state = media_server
    target = tmp_path / "out.mp4"
    target.write_bytes(state["data"])  # already fully on disk, just never committed

    totals = []
    n = download_file(requests.Session(), url, target, total_cb=totals.append)

    assert n == len(state["data"])
    assert target.read_bytes() == state["data"]  # untouched, not re-fetched
    assert totals == [len(state["data"])]


def test_download_file_treats_a_flaky_500_at_eof_as_already_complete_too(media_server, tmp_path):
    """The 416 fix alone isn't enough: a real server was observed answering
    the identical at-EOF resume with 416 from one backend worker and a bogus
    500 from another, for the same fully-downloaded file. A resume must not
    depend on which one happens to answer — it should verify the true size
    directly and accept the file either way."""
    url, state = media_server
    state["mode"] = "flaky_500_at_eof"
    target = tmp_path / "out.mp4"
    target.write_bytes(state["data"])

    totals = []
    n = download_file(requests.Session(), url, target, total_cb=totals.append)

    assert n == len(state["data"])
    assert target.read_bytes() == state["data"]
    assert totals == [len(state["data"])]


def test_download_file_still_fails_a_genuine_500_on_a_fresh_download(media_server, tmp_path):
    """The at-EOF-recovery fallback must not paper over a real server error
    on a normal (non-resume) download — nothing is on disk yet, so there's
    nothing it could possibly already have."""
    url, state = media_server
    state["mode"] = "fail_500"
    target = tmp_path / "out.mp4"  # no existing partial: resume_from stays 0

    with pytest.raises(DownloadError):
        download_file(requests.Session(), url, target)


def test_download_file_converts_local_storage_error_preparing_target(media_server, tmp_path):
    """A network-mounted target directory going unreachable (CIFS/NFS host
    down) surfaces as a bare OSError, not a requests exception — it must be
    converted to a DownloadError so the engine's normal retry/failure
    handling applies instead of an unhandled crash."""
    url, _state = media_server
    target = tmp_path / "out.mp4"
    with (
        patch("pathlib.Path.mkdir", side_effect=OSError(112, "Host is down")),
        pytest.raises(DownloadError, match="local storage error"),
    ):
        download_file(requests.Session(), url, target)


def test_download_file_converts_local_storage_error_while_writing(media_server, tmp_path):
    url, _state = media_server
    target = tmp_path / "out.mp4"
    with (
        patch("pathlib.Path.open", side_effect=OSError(112, "Host is down")),
        pytest.raises(DownloadError, match="local storage error"),
    ):
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
def test_engine_run_forwards_total_cb(media_server, tmp_path):
    url, state = media_server
    target = tmp_path / "out.mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")
    totals = []

    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store, verify_mode="quick")
        engine.run(job, total_cb=totals.append)

    assert totals == [len(state["data"])]


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


def test_engine_run_skips_still_fires_start_and_complete_callbacks(media_server, tmp_path):
    """An already-complete file should be reported as immediately complete
    rather than left invisible to the progress UI."""
    url, _state = media_server
    target = tmp_path / "out.mp4"
    target.write_bytes(b"already here")

    starts = []
    completions = []
    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store)
        job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")
        ok = engine.run(
            job, start_cb=lambda: starts.append(True), complete_cb=completions.append
        )

    assert ok is True
    assert starts == [True]
    assert completions == [True]


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_calls_start_once_and_complete_true_on_success(media_server, tmp_path):
    url, _state = media_server
    target = tmp_path / "out.mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")

    starts = []
    completions = []
    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(requests.Session(), state_store, verify_mode="quick")
        ok = engine.run(
            job, start_cb=lambda: starts.append(True), complete_cb=completions.append
        )

    assert ok is True
    assert starts == [True]  # once, not once per retry attempt
    assert completions == [True]


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_survives_local_storage_error_finalizing_the_download(media_server, tmp_path):
    """The transfer and verify succeed, but committing the file (mkdir +
    os.replace onto the final, possibly network-mounted, target) fails with
    an OSError — must be reported as a normal failed job, not crash the run."""
    url, _state = media_server
    target = tmp_path / "Movies" / "Example (2024)" / "Example (2024).mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")

    completions = []
    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(
            requests.Session(), state_store, verify_mode="quick", max_attempts=1
        )
        with patch("xc_vod_dl.download.engine.os.replace", side_effect=OSError(112, "Host is down")):
            ok = engine.run(job, complete_cb=completions.append)
        record = state_store.get("movie:1")

    assert ok is False
    assert completions == [False]
    assert record.status == "failed"
    assert "local storage error" in record.last_error


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH")
def test_engine_run_calls_start_once_and_complete_false_on_exhausted_failure(media_server, tmp_path):
    url, state = media_server
    state["data"] = b"not a real media file at all"
    target = tmp_path / "out.mp4"
    job = DownloadJob(id="movie:1", url=url, target_path=target, kind="movie", title="Example")

    starts = []
    completions = []
    with StateStore(":memory:") as state_store:
        engine = DownloadEngine(
            requests.Session(), state_store, verify_mode="quick", max_attempts=2
        )
        ok = engine.run(
            job, start_cb=lambda: starts.append(True), complete_cb=completions.append
        )

    assert ok is False
    assert starts == [True]  # still exactly once despite two failed attempts
    assert completions == [False]


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
