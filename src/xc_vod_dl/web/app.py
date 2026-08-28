from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.config import Config
from xc_vod_dl.exceptions import XcVodDlError
from xc_vod_dl.gaps import (
    detect_duplicate_episodes_in_series,
    detect_gaps_in_series,
    format_gap_report,
)
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.ui.interactive import _cross_series_gap_maps, _sample_resolution
from xc_vod_dl.web.runner import JobRunner

STATIC_DIR = Path(__file__).parent / "static"


class DownloadRequest(BaseModel):
    kind: str  # "movie" | "series"
    id: int
    season: int | None = None
    episode: int | None = None
    display_name: str | None = None


def create_app(config: Config) -> FastAPI:
    """Builds the web UI app around the exact same client/engine/state
    modules the CLI uses — this is another front end onto xc_vod_dl, not a
    reimplementation of its download logic. One process, one XtreamClient,
    one StateStore, one JobRunner shared across all requests."""
    client = XtreamClient(config.account.server, config.account.username, config.account.password)
    state = StateStore(config.download.state_db)
    runner = JobRunner(client, config, state)

    app = FastAPI(title="xc-vod-dl")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/movies/search")
    def search_movies(q: str) -> list[dict[str, Any]]:
        if not q:
            return []
        categories = {c.category_id: c.category_name for c in client.get_vod_categories()}
        q_lower = q.lower()
        matches = [s for s in client.get_vod_streams() if q_lower in s.name.lower()][:40]

        results = []
        for s in matches:
            duration = None
            try:
                duration = client.get_vod_info(s.stream_id).duration
            except XcVodDlError:
                pass
            results.append(
                {
                    "id": s.stream_id,
                    "name": s.name,
                    "category": categories.get(s.category_id, s.category_id),
                    "container_extension": s.container_extension,
                    "year": s.year,
                    "duration": duration,
                }
            )
        return results

    @app.get("/api/series/search")
    def search_series(q: str) -> list[dict[str, Any]]:
        if not q:
            return []
        categories = {c.category_id: c.category_name for c in client.get_series_categories()}
        q_lower = q.lower()
        matches = [s for s in client.get_series_streams() if q_lower in s.name.lower()][:40]

        info_by_id = {}
        for s in matches:
            try:
                info_by_id[s.series_id] = client.get_series_info(s.series_id)
            except XcVodDlError:
                info_by_id[s.series_id] = None

        cross_gap_by_id = _cross_series_gap_maps(matches, info_by_id)

        results = []
        for s in matches:
            info = info_by_id.get(s.series_id)
            entry: dict[str, Any] = {
                "id": s.series_id,
                "name": s.name,
                "category": categories.get(s.category_id, s.category_id),
            }
            if info is not None:
                gap_map = cross_gap_by_id.get(s.series_id) or detect_gaps_in_series(info)
                dupe_map = detect_duplicate_episodes_in_series(info)
                entry["seasons"] = sorted(info.episodes)
                entry["episode_count"] = sum(len(eps) for eps in info.episodes.values())
                entry["resolution"] = _sample_resolution(info)
                entry["gaps"] = gap_map
                entry["duplicates"] = dupe_map
                entry["report"] = (
                    format_gap_report(s.name, gap_map, dupe_map) if (gap_map or dupe_map) else None
                )
            else:
                entry["seasons"] = None
                entry["error"] = "could not load season info"
            results.append(entry)
        return results

    @app.post("/api/download", status_code=202)
    def queue_download(requests_: list[DownloadRequest]) -> dict[str, Any]:
        specs = [
            JobSpec(
                kind=r.kind,  # type: ignore[arg-type]
                id=r.id,
                season=r.season,
                episode=r.episode,
                display_name=r.display_name or None,
            )
            for r in requests_
        ]
        runner.submit(specs)
        return {"queued": len(specs)}

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        return runner.jobs

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        q = runner.subscribe()

        async def stream():
            try:
                # Replay the current snapshot first so a client that
                # connects mid-run isn't blind to jobs already in flight.
                yield f"event: snapshot\ndata: {json.dumps(runner.jobs)}\n\n"
                loop = asyncio.get_running_loop()
                while True:
                    try:
                        event = await loop.run_in_executor(None, q.get, True, 15)
                    except queue.Empty:
                        yield ": keep-alive\n\n"
                        continue
                    payload = {"job_id": event.job_id, **event.data}
                    yield f"event: {event.type}\ndata: {json.dumps(payload)}\n\n"
            finally:
                runner.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
