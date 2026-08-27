from __future__ import annotations

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)


class ProgressReporter:
    """One live row per in-flight download, fed by the same (job_id, bytes)
    callback the download engine already calls for the concurrency
    controller's throughput tracking — no separate progress-tracking path.

    A row only appears once a job's transfer actually begins (start()) —
    not when it's merely queued behind others in a serial run or behind the
    concurrency ceiling in a parallel one. Without that, every job's
    indeterminate-total "pulse" animation and elapsed-time clock would start
    the moment the whole batch was handed to the engine, not the moment that
    particular item started moving bytes.

    Total size is unknown (shows as "?", indeterminate/pulsing bar) until the
    server's Content-Length header tells us otherwise — see set_total()/
    total_cb in download/engine.py. Not every server sends one, so it may
    never fire; complete() forces the bar to a definite 100% regardless.
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[title]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("{task.fields[status]}"),
        )
        self._task_ids: dict[str, int] = {}

    def __enter__(self) -> ProgressReporter:
        self._progress.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        self._progress.__exit__(*exc_info)

    def start(self, job_id: str, title: str) -> None:
        self._task_ids[job_id] = self._progress.add_task("", title=title, status="", total=None)

    def report(self, job_id: str, n: int) -> None:
        task_id = self._task_ids.get(job_id)
        if task_id is not None:
            self._progress.update(task_id, advance=n)

    def set_total(self, job_id: str, total: int) -> None:
        task_id = self._task_ids.get(job_id)
        if task_id is not None:
            self._progress.update(task_id, total=total)

    def complete(self, job_id: str, success: bool) -> None:
        """Freezes the row at its final state: a definite (non-pulsing) full
        bar, a stopped elapsed clock, and an explicit done/failed label —
        even for jobs whose total size was never known."""
        task_id = self._task_ids.get(job_id)
        if task_id is None:
            return
        task = next(t for t in self._progress.tasks if t.id == task_id)
        total = task.total if task.total is not None else task.completed
        status = "[green]done[/green]" if success else "[red]failed[/red]"
        self._progress.update(task_id, total=total, completed=total, status=status)
        self._progress.stop_task(task_id)
