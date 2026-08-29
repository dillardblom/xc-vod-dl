from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any

from xc_vod_dl.api.client import XtreamClient, new_session
from xc_vod_dl.cli import _resolve_jobs, _run_jobs
from xc_vod_dl.config import Config
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
    it. One background thread processes submitted batches serially (a
    batch itself still downloads its items with real parallelism via the
    concurrency controller); a second batch queued while one is running
    just waits its turn.
    """

    def __init__(self, client: XtreamClient, config: Config, state: StateStore):
        self.client = client
        self.config = config
        self.state = state
        self.jobs: dict[str, dict[str, Any]] = {}
        self._bus = EventBus()
        self._queue: queue.Queue[list[JobSpec]] = queue.Queue()
        self._session = new_session()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, specs: list[JobSpec]) -> None:
        self._queue.put(specs)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[ProgressEvent]:
        return self._bus.subscribe(loop)

    def unsubscribe(self, q: asyncio.Queue[ProgressEvent]) -> None:
        self._bus.unsubscribe(q)

    def _loop(self) -> None:
        while True:
            specs = self._queue.get()
            try:
                self._run_batch(specs)
            except Exception:
                logger.exception("web download batch failed")
                self._bus.publish(ProgressEvent("log", "*", {"message": "batch failed, see server log"}))

    def _run_batch(self, specs: list[JobSpec]) -> None:
        jobs, writers = _resolve_jobs(self.client, self.config, self._session, specs)
        if not jobs:
            # _resolve_jobs already logged a per-spec warning (e.g. a
            # timeout reaching the server) — without this, the web UI would
            # otherwise show nothing at all happening, indistinguishable
            # from the request never having been received.
            self._bus.publish(
                ProgressEvent("log", "*", {"message": "nothing could be resolved, see server log"})
            )
            return
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
