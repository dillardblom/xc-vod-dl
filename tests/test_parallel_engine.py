import shutil
import threading

import pytest
import requests

from xc_vod_dl.download.concurrency import ConcurrencyController
from xc_vod_dl.download.engine import DownloadJob, run_many
from xc_vod_dl.state.store import StateStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH"),
]


def test_run_many_downloads_all_jobs_successfully(media_server, tmp_path):
    url, _state = media_server
    jobs = [
        DownloadJob(
            id=f"movie:{i}",
            url=url,
            target_path=tmp_path / f"movie{i}.mp4",
            kind="movie",
            title=f"Movie {i}",
        )
        for i in range(5)
    ]

    with StateStore(":memory:") as state:
        controller = ConcurrencyController(initial=3, maximum=3)
        results = run_many(
            jobs,
            session_factory=requests.Session,
            state=state,
            controller=controller,
        )

        assert all(results.values())
        assert set(results) == {j.id for j in jobs}
        for job in jobs:
            assert job.target_path.exists()
            assert state.get(job.id).status == "done"


def test_run_many_respects_the_controller_ceiling(media_server, tmp_path):
    """The server holds each request open for a bit (delay_s) and tracks true
    concurrent in-flight requests. With the controller pinned to 1 (serial),
    even a 4-worker thread pool must never let more than 1 request overlap."""
    url, state = media_server
    state["delay_s"] = 0.05

    jobs = [
        DownloadJob(
            id=f"movie:{i}",
            url=url,
            target_path=tmp_path / f"movie{i}.mp4",
            kind="movie",
            title=f"Movie {i}",
        )
        for i in range(4)
    ]

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=1, maximum=4)
        results = run_many(
            jobs,
            session_factory=requests.Session,
            state=store,
            controller=controller,
        )

    assert all(results.values())
    assert state["max_active"] == 1


def test_run_many_allows_real_overlap_when_limit_is_higher(media_server, tmp_path):
    url, state = media_server
    state["delay_s"] = 0.05

    jobs = [
        DownloadJob(
            id=f"movie:{i}",
            url=url,
            target_path=tmp_path / f"movie{i}.mp4",
            kind="movie",
            title=f"Movie {i}",
        )
        for i in range(4)
    ]

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=3, maximum=3)
        results = run_many(
            jobs,
            session_factory=requests.Session,
            state=store,
            controller=controller,
        )

    assert all(results.values())
    assert state["max_active"] > 1


def test_run_many_uses_a_separate_session_per_worker(media_server, tmp_path):
    url, _state = media_server
    seen_sessions = []
    lock = threading.Lock()

    def factory():
        s = requests.Session()
        with lock:
            seen_sessions.append(s)
        return s

    jobs = [
        DownloadJob(
            id=f"movie:{i}",
            url=url,
            target_path=tmp_path / f"movie{i}.mp4",
            kind="movie",
            title=f"Movie {i}",
        )
        for i in range(3)
    ]

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=3, maximum=3)
        run_many(jobs, session_factory=factory, state=store, controller=controller)

    assert len(seen_sessions) == 3
    assert len({id(s) for s in seen_sessions}) == 3


def test_run_many_reports_progress_per_job(media_server, tmp_path):
    url, _state = media_server
    jobs = [
        DownloadJob(
            id=f"movie:{i}", url=url, target_path=tmp_path / f"movie{i}.mp4", kind="movie", title="x"
        )
        for i in range(3)
    ]
    progress: dict[str, int] = {}
    lock = threading.Lock()

    def progress_cb(job_id: str, n: int) -> None:
        with lock:
            progress[job_id] = progress.get(job_id, 0) + n

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=3, maximum=3)
        run_many(
            jobs,
            session_factory=requests.Session,
            state=store,
            controller=controller,
            progress_cb=progress_cb,
        )

    assert set(progress) == {j.id for j in jobs}
    assert all(n > 0 for n in progress.values())


def test_run_many_reports_total_per_job(media_server, tmp_path):
    url, state = media_server
    jobs = [
        DownloadJob(
            id=f"movie:{i}", url=url, target_path=tmp_path / f"movie{i}.mp4", kind="movie", title="x"
        )
        for i in range(3)
    ]
    totals: dict[str, int] = {}
    lock = threading.Lock()

    def total_cb(job_id: str, n: int) -> None:
        with lock:
            totals[job_id] = n

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=3, maximum=3)
        run_many(
            jobs,
            session_factory=requests.Session,
            state=store,
            controller=controller,
            total_cb=total_cb,
        )

    assert totals == {j.id: len(state["data"]) for j in jobs}


def test_run_many_forwards_start_and_complete_per_job(media_server, tmp_path):
    url, _state = media_server
    jobs = [
        DownloadJob(
            id=f"movie:{i}", url=url, target_path=tmp_path / f"movie{i}.mp4", kind="movie", title="x"
        )
        for i in range(3)
    ]
    started: dict[str, str] = {}
    completed: dict[str, bool] = {}
    lock = threading.Lock()

    def start_cb(job_id: str, title: str) -> None:
        with lock:
            started[job_id] = title

    def complete_cb(job_id: str, ok: bool) -> None:
        with lock:
            completed[job_id] = ok

    with StateStore(":memory:") as store:
        controller = ConcurrencyController(initial=3, maximum=3)
        run_many(
            jobs,
            session_factory=requests.Session,
            state=store,
            controller=controller,
            start_cb=start_cb,
            complete_cb=complete_cb,
        )

    assert started == {j.id: j.title for j in jobs}
    assert completed == {j.id: True for j in jobs}
