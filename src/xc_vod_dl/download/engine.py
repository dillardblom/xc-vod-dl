from __future__ import annotations

import concurrent.futures
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from xc_vod_dl.download.concurrency import ConcurrencyController, TransferOutcome
from xc_vod_dl.download.verify import VerifyMode, verify_media
from xc_vod_dl.exceptions import DownloadError
from xc_vod_dl.state.store import StateStore

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int], None]

_KIND_TO_OUTCOME = {
    "timeout": TransferOutcome.TIMEOUT,
    "conn_reset": TransferOutcome.CONN_RESET,
    "throttle": TransferOutcome.HTTP_THROTTLE,
    "other": TransferOutcome.OTHER_ERROR,
}


@dataclass
class DownloadJob:
    id: str  # state store key, e.g. "movie:101" or "episode:9001"
    url: str
    target_path: Path
    kind: str  # "movie" | "episode"
    title: str
    series_id: str | None = None
    season: int | None = None
    episode_num: int | None = None
    container_extension: str | None = None


def tmp_path_for(final_path: Path) -> Path:
    """In-progress files carry a `.voddl` marker suffix — a `.voddl` file left
    in a folder is known-incomplete/unverified and safe to ignore or clean up."""
    return final_path.with_name(final_path.name + ".voddl")


def download_file(
    session: requests.Session,
    url: str,
    tmp_path: Path,
    *,
    chunk_size: int = 1 << 16,
    progress_cb: ProgressCallback | None = None,
    total_cb: Callable[[int], None] | None = None,
    timeout: float = 30.0,
) -> int:
    """Download `url` into `tmp_path`, resuming from any existing partial
    content already on disk. Returns the total bytes on disk when done.

    Raises DownloadError on a network failure or unexpected HTTP status —
    the partial file is left in place so a subsequent call can resume it,
    except when the server ignores our Range request, in which case the
    stale partial is discarded (it can't be trusted as a resume base).

    `total_cb`, if given, is called once with the expected final file size in
    bytes as soon as it's known from the response's Content-Length header —
    servers don't always send one, so it may never fire.
    """
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    resume_from = tmp_path.stat().st_size if tmp_path.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}

    try:
        resp = session.get(url, headers=headers, stream=True, timeout=timeout)
    except requests.Timeout as exc:
        raise DownloadError(f"timed out requesting {url}: {exc}", kind="timeout") from exc
    except requests.ConnectionError as exc:
        raise DownloadError(f"connection error requesting {url}: {exc}", kind="conn_reset") from exc
    except requests.RequestException as exc:
        raise DownloadError(f"request to {url} failed: {exc}", kind="other") from exc

    if resume_from > 0 and resp.status_code == 200:
        # Server doesn't support (or ignored) range requests for this URL —
        # our partial can't be trusted as a prefix of this response.
        resp.close()
        tmp_path.unlink(missing_ok=True)
        return download_file(
            session,
            url,
            tmp_path,
            chunk_size=chunk_size,
            progress_cb=progress_cb,
            total_cb=total_cb,
            timeout=timeout,
        )

    if resp.status_code in (503, 429):
        resp.close()
        raise DownloadError(f"throttled: HTTP {resp.status_code} for {url}", kind="throttle")

    if resp.status_code == 403 and resume_from > 0:
        # A 403 on what should have been a resume continuation reads as
        # server-side pushback, not a first-contact auth failure.
        resp.close()
        raise DownloadError(f"HTTP 403 resuming {url}", kind="throttle")

    if resp.status_code not in (200, 206):
        resp.close()
        raise DownloadError(f"unexpected HTTP {resp.status_code} for {url}", kind="other")

    mode = "ab" if resp.status_code == 206 else "wb"
    bytes_on_disk = resume_from if mode == "ab" else 0

    if total_cb is not None:
        content_length = resp.headers.get("Content-Length")
        if content_length is not None:
            try:
                # A 206 response's Content-Length is only the remaining bytes.
                total_cb(bytes_on_disk + int(content_length))
            except ValueError:
                pass

    try:
        with tmp_path.open(mode) as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                bytes_on_disk += len(chunk)
                if progress_cb:
                    progress_cb(len(chunk))
    except requests.Timeout as exc:
        raise DownloadError(f"timed out streaming {url}: {exc}", kind="timeout") from exc
    except requests.ConnectionError as exc:
        raise DownloadError(f"connection error streaming {url}: {exc}", kind="conn_reset") from exc
    except requests.RequestException as exc:
        raise DownloadError(f"stream interrupted for {url}: {exc}", kind="other") from exc
    finally:
        resp.close()

    return bytes_on_disk


class DownloadEngine:
    """Drives one item (movie or episode) through download -> verify -> commit,
    with bounded retries and state tracking. Not concurrency-aware by itself —
    see download/concurrency.py for parallel orchestration on top of this."""

    def __init__(
        self,
        session: requests.Session,
        state: StateStore,
        *,
        verify_mode: VerifyMode = "quick",
        ffprobe_path: str | None = None,
        ffmpeg_path: str | None = None,
        max_attempts: int = 3,
        chunk_size: int = 1 << 16,
        controller: ConcurrencyController | None = None,
    ):
        self.session = session
        self.state = state
        self.verify_mode = verify_mode
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.max_attempts = max_attempts
        self.chunk_size = chunk_size
        self.controller = controller

    def run(
        self,
        job: DownloadJob,
        progress_cb: ProgressCallback | None = None,
        total_cb: Callable[[int], None] | None = None,
        start_cb: Callable[[], None] | None = None,
        complete_cb: Callable[[bool], None] | None = None,
    ) -> bool:
        """Returns True if the job ends up done (already-existing files count),
        False if every attempt was exhausted without producing a verified file.

        `start_cb`, if given, fires exactly once, right as the actual transfer
        begins — not when the job is merely queued behind others (serially or
        behind the concurrency ceiling). Progress UI hangs off this rather
        than the moment the job was handed to the engine, so a queued item's
        indeterminate-progress "pulse" and elapsed-time clock only start once
        it's really downloading. `complete_cb`, if given, fires exactly once
        at the end with the final success/failure outcome.
        """
        if job.target_path.exists():
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
            self.state.mark_status(job.id, "done")
            logger.info("%s already complete: %s", job.id, job.title)
            if start_cb:
                start_cb()
            if complete_cb:
                complete_cb(True)
            return True

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

        tmp_path = tmp_path_for(job.target_path)

        def combined_progress(n: int) -> None:
            if self.controller is not None:
                self.controller.record_bytes(job.id, n)
            if progress_cb:
                progress_cb(n)

        if start_cb:
            start_cb()
        logger.info("starting download: %s (%s)", job.id, job.title)

        ok = False
        for attempt in range(1, self.max_attempts + 1):
            self.state.mark_status(job.id, "downloading", increment_attempts=True)
            if self.controller is not None:
                self.controller.acquire()
            try:
                bytes_on_disk = download_file(
                    self.session,
                    job.url,
                    tmp_path,
                    chunk_size=self.chunk_size,
                    progress_cb=combined_progress,
                    total_cb=total_cb,
                )
            except DownloadError as exc:
                if self.controller is not None:
                    self.controller.report_outcome(job.id, _KIND_TO_OUTCOME[exc.kind])
                self.state.mark_status(job.id, "failed", last_error=str(exc))
                if attempt == self.max_attempts:
                    break
                continue  # tmp_path (if any bytes landed) persists on disk for the next attempt to resume
            else:
                if self.controller is not None:
                    self.controller.report_outcome(job.id, TransferOutcome.OK)
            finally:
                if self.controller is not None:
                    self.controller.release()

            self.state.mark_status(job.id, "verifying", bytes_downloaded=bytes_on_disk)
            result = verify_media(
                tmp_path,
                mode=self.verify_mode,
                ffprobe_path=self.ffprobe_path,
                ffmpeg_path=self.ffmpeg_path,
            )
            if result.ok:
                if result.warning:
                    logger.info("verify warning for %s (accepted): %s", job.id, result.warning)
                job.target_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, job.target_path)
                self.state.mark_status(job.id, "done", bytes_downloaded=bytes_on_disk)
                ok = True
                break

            # A file that fails verification can't be trusted as a resume base —
            # purge it and retry with a clean download, rather than the old
            # "just delete it and give up" behavior.
            tmp_path.unlink(missing_ok=True)
            self.state.mark_status(
                job.id, "failed", last_error=f"verification failed: {result.reason}"
            )
            if attempt == self.max_attempts:
                break

        logger.info("finished download: %s -> %s", job.id, "done" if ok else "failed")
        if complete_cb:
            complete_cb(ok)
        return ok


def run_many(
    jobs: list[DownloadJob],
    *,
    session_factory: Callable[[], requests.Session],
    state: StateStore,
    controller: ConcurrencyController,
    verify_mode: VerifyMode = "quick",
    ffprobe_path: str | None = None,
    ffmpeg_path: str | None = None,
    max_attempts: int = 3,
    chunk_size: int = 1 << 16,
    progress_cb: Callable[[str, int], None] | None = None,
    total_cb: Callable[[str, int], None] | None = None,
    start_cb: Callable[[str, str], None] | None = None,
    complete_cb: Callable[[str, bool], None] | None = None,
) -> dict[str, bool]:
    """Run `jobs` through `controller`'s dynamically-sized pool.

    `ThreadPoolExecutor(max_workers=controller.maximum)` is the hard ceiling;
    `controller.acquire()`/`release()` inside each DownloadEngine.run() is the
    soft, dynamically-adjusted gate underneath it. Each worker thread gets its
    own requests.Session (via `session_factory`) and its own DownloadEngine —
    Sessions aren't safe to share across threads. `state` is a single shared
    StateStore; it's internally lock-protected, so sharing it here is safe.

    Returns a mapping of job.id -> success.
    """

    def worker(job: DownloadJob) -> tuple[str, bool]:
        engine = DownloadEngine(
            session_factory(),
            state,
            verify_mode=verify_mode,
            ffprobe_path=ffprobe_path,
            ffmpeg_path=ffmpeg_path,
            max_attempts=max_attempts,
            chunk_size=chunk_size,
            controller=controller,
        )
        cb = (lambda n, jid=job.id: progress_cb(jid, n)) if progress_cb else None
        total = (lambda n, jid=job.id: total_cb(jid, n)) if total_cb else None
        start = (lambda jid=job.id, title=job.title: start_cb(jid, title)) if start_cb else None
        complete = (lambda ok, jid=job.id: complete_cb(jid, ok)) if complete_cb else None
        return job.id, engine.run(
            job, progress_cb=cb, total_cb=total, start_cb=start, complete_cb=complete
        )

    results: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=controller.maximum) as pool:
        for job_id, ok in pool.map(worker, jobs):
            results[job_id] = ok
    return results
