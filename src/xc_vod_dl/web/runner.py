from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any

from xc_vod_dl.api.client import XtreamClient, new_session
from xc_vod_dl.cli import _resolve_jobs, _run_jobs
from xc_vod_dl.config import Config
from xc_vod_dl.download.engine import DownloadJob
from xc_vod_dl.exceptions import XcVodDlError
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.web.reporter import EventBus, ProgressEvent, WebProgressReporter

logger = logging.getLogger(__name__)


class JobRunner:
    """Runs submitted JobSpec batches through the exact same
    resolve -> register -> DownloadEngine/ConcurrencyController pipeline
    the CLI's fetch/browse commands use (cli._resolve_jobs / cli._run_jobs)
    — the web UI is another front end onto that, not a reimplementation of
    it.

    Two background threads, not one, deliberately: a *resolver* thread
    turns each submitted batch of JobSpecs into real DownloadJobs (network
    calls to get_vod_info/get_series_info) and registers them as "pending"
    immediately; a separate *downloader* thread actually transfers them,
    one resolved batch at a time, with real parallelism inside a batch via
    the concurrency controller. Splitting these matters because the
    downloader blocks for as long as its batch takes — for a whole series
    that can be a long time. With one combined thread (the original
    design), a second submission made while a first was still downloading
    sat invisible in an in-memory queue with no state.db record and no UI
    presence at all until the first batch fully finished — confirmed live
    against a real account: a second series queued behind a long-running
    one didn't show up in `status` or the web UI at all. The resolver
    being separate means a new submission gets registered (and so becomes
    visible as "pending") within moments regardless of what the downloader
    is doing — it just won't start moving bytes until its turn comes.
    """

    def __init__(self, client: XtreamClient, config: Config, state: StateStore):
        self.client = client
        self.config = config
        self.state = state
        self.jobs: dict[str, dict[str, Any]] = {}
        self._bus = EventBus()
        self._submit_queue: queue.Queue[list[JobSpec]] = queue.Queue()
        self._download_queue: queue.Queue[list[DownloadJob]] = queue.Queue()
        self._session = new_session()
        self._resolver_thread = threading.Thread(target=self._resolve_loop, daemon=True)
        self._resolver_thread.start()
        self._downloader_thread = threading.Thread(target=self._download_loop, daemon=True)
        self._downloader_thread.start()

    def submit(self, specs: list[JobSpec]) -> None:
        self._submit_queue.put(specs)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[ProgressEvent]:
        return self._bus.subscribe(loop)

    def unsubscribe(self, q: asyncio.Queue[ProgressEvent]) -> None:
        self._bus.unsubscribe(q)

    def _resolve_loop(self) -> None:
        while True:
            specs = self._submit_queue.get()
            try:
                jobs = self._resolve_and_register(specs)
            except Exception:
                logger.exception("web batch resolve failed")
                self._bus.publish(
                    ProgressEvent("log", "*", {"message": "batch failed to resolve, see server log"})
                )
                continue
            if jobs:
                self._download_queue.put(jobs)

    def _resolve_and_register(self, specs: list[JobSpec]) -> list[DownloadJob]:
        jobs, writers = _resolve_jobs(self.client, self.config, self._session, specs)
        if not jobs:
            # _resolve_jobs already logged a per-spec warning (e.g. a
            # timeout reaching the server) — without this, the web UI would
            # otherwise show nothing at all happening, indistinguishable
            # from the request never having been received.
            self._bus.publish(
                ProgressEvent("log", "*", {"message": "nothing could be resolved, see server log"})
            )
            return []
        for writer in writers:
            writer()
        for job in jobs:
            self.state.upsert_pending(
                id=job.id,
                kind=job.kind,
                title=job.title,
                target_path=str(job.target_path),
                series_id=job.series_id,
                season=job.season,
                episode_num=job.episode_num,
                container_extension=job.container_extension,
            )
            # Visible as "pending" the moment it's registered, not only once
            # its download batch reaches the front of the downloader's
            # queue — start()/WebProgressReporter overwrites this wholesale
            # with a "downloading" entry once that actually happens, so
            # there's no stale-state risk from setting it eagerly here.
            self.jobs[job.id] = {
                "title": job.title,
                "completed": 0,
                "total": None,
                "status": "pending",
            }
            self._bus.publish(ProgressEvent("queued", job.id, {"title": job.title}))
        return jobs

    def _download_loop(self) -> None:
        while True:
            jobs = self._download_queue.get()
            try:
                self._download(jobs)
            except Exception:
                logger.exception("web download batch failed")
                self._bus.publish(ProgressEvent("log", "*", {"message": "batch failed, see server log"}))

    def _download(self, jobs: list[DownloadJob]) -> None:
        try:
            account = self.client.get_account()
        except XcVodDlError:
            self._bus.publish(ProgressEvent("log", "*", {"message": "could not reach server"}))
            return

        reporter = WebProgressReporter(self.jobs, self._bus.publish)
        _run_jobs(
            self.client,
            self.config,
            account,
            self._session,
            jobs,
            self.state,
            serial=False,
            parallel_override=None,
            verify_mode=self.config.download.verify_mode,
            reporter=reporter,
        )
