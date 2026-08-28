from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProgressEvent:
    type: str  # "start" | "progress" | "total" | "complete" | "log"
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)


class WebProgressReporter:
    """Same duck-typed interface cli.py's _run_jobs expects from a
    ui.progress.ProgressReporter (start/report/set_total/complete) — a
    drop-in for the web UI instead of the terminal one. Publishes each
    change as a ProgressEvent to any number of SSE subscribers and keeps a
    running snapshot in `jobs` for clients that connect mid-run.
    """

    def __init__(self, jobs: dict[str, dict[str, Any]], publish: Callable[[ProgressEvent], None]):
        self._jobs = jobs
        self._publish = publish
        self._lock = threading.Lock()

    def start(self, job_id: str, title: str) -> None:
        with self._lock:
            self._jobs[job_id] = {
                "title": title,
                "completed": 0,
                "total": None,
                "status": "downloading",
            }
        self._publish(ProgressEvent("start", job_id, {"title": title}))

    def report(self, job_id: str, n: int) -> None:
        with self._lock:
            meta = self._jobs.setdefault(
                job_id, {"title": job_id, "completed": 0, "total": None, "status": "downloading"}
            )
            meta["completed"] += n
            completed = meta["completed"]
        self._publish(ProgressEvent("progress", job_id, {"completed": completed}))

    def set_total(self, job_id: str, total: int) -> None:
        with self._lock:
            self._jobs.setdefault(
                job_id, {"title": job_id, "completed": 0, "total": None, "status": "downloading"}
            )["total"] = total
        self._publish(ProgressEvent("total", job_id, {"total": total}))

    def complete(self, job_id: str, success: bool) -> None:
        status = "done" if success else "failed"
        with self._lock:
            self._jobs.setdefault(
                job_id, {"title": job_id, "completed": 0, "total": None}
            )["status"] = status
        self._publish(ProgressEvent("complete", job_id, {"ok": success}))


class EventBus:
    """Simple in-memory pub/sub: each SSE connection gets its own Queue,
    fed by publish() from whichever thread is actually running downloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[ProgressEvent]] = []

    def subscribe(self) -> queue.Queue[ProgressEvent]:
        q: queue.Queue[ProgressEvent] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[ProgressEvent]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: ProgressEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)
