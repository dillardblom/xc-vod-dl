import asyncio
import threading
import time

import pytest

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.config import AccountConfig, Config, DownloadConfig
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.web.runner import JobRunner

pytestmark = pytest.mark.integration


def test_run_batch_publishes_a_log_event_when_nothing_resolves(xtream_server):
    """A batch where every spec fails to resolve (e.g. an unknown vod_id)
    used to return silently — the web UI showed nothing at all happening,
    indistinguishable from the request never having been received."""
    base_url, _state = xtream_server
    client = XtreamClient(base_url, "demo", "demo")
    config = Config(account=AccountConfig(server=base_url, username="demo", password="demo"))
    state = StateStore(":memory:")
    runner = JobRunner(client, config, state)

    async def scenario():
        q = runner.subscribe(asyncio.get_running_loop())
        runner.submit([JobSpec(kind="movie", id=999999)])  # never registered -> 404
        event = await asyncio.wait_for(q.get(), timeout=5)
        assert event.type == "log"
        assert "nothing could be resolved" in event.data["message"]

    asyncio.run(scenario())


def test_second_batch_is_visible_while_first_is_still_downloading(xtream_server, monkeypatch, tmp_path):
    """The original JobRunner design had one worker thread doing both
    resolve+register and the actual (slow) download, serially per batch —
    a second submission made while a first batch was still downloading sat
    in an in-memory queue with no state.db record and no UI presence at
    all until the first fully finished. Confirmed against a real account:
    a series queued behind a long-running one didn't show up in `status`
    or the web UI at all. This exercises the fix: resolve+register happens
    on its own thread, independent of how long any download takes."""
    base_url, state = xtream_server
    for vod_id, name in ((101, "Movie A"), (102, "Movie B")):
        state["vod_info"][str(vod_id)] = {
            "info": {"name": name, "releasedate": "2024-01-01"},
            "movie_data": {
                "stream_id": vod_id,
                "name": name,
                "container_extension": "mp4",
                "category_id": "1",
            },
        }
    client = XtreamClient(base_url, "demo", "demo")
    config = Config(
        account=AccountConfig(server=base_url, username="demo", password="demo"),
        download=DownloadConfig(movies_dir=tmp_path / "Movies", series_dir=tmp_path / "Series"),
    )
    store = StateStore(":memory:")

    first_batch_started = threading.Event()
    release_first_batch = threading.Event()

    def fake_run_jobs(client, config, account, session, jobs, state, *, serial, parallel_override, verify_mode, reporter):
        first_batch_started.set()
        assert release_first_batch.wait(timeout=5), "test never released the fake slow download"
        return dict.fromkeys((j.id for j in jobs), True)

    monkeypatch.setattr("xc_vod_dl.web.runner._run_jobs", fake_run_jobs)

    runner = JobRunner(client, config, store)
    try:
        runner.submit([JobSpec(kind="movie", id=101)])
        assert first_batch_started.wait(timeout=5), "first batch's (fake) download never started"

        # Second batch submitted *while the first is still "downloading"* —
        # it must be registered (and so visible) promptly, not only after
        # the first batch's fake_run_jobs call returns.
        runner.submit([JobSpec(kind="movie", id=102)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            entry = runner.jobs.get("movie:102")
            if entry is not None and entry["status"] == "pending":
                break
            time.sleep(0.05)
        else:
            pytest.fail("second batch never became visible while the first was still downloading")

        assert store.get("movie:102") is not None
        assert store.get("movie:102").status == "pending"
    finally:
        release_first_batch.set()
