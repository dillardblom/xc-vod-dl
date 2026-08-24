from __future__ import annotations

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)


class ProgressReporter:
    """One live row per in-flight download, fed by the same (job_id, bytes)
    callback the download engine already calls for the concurrency
    controller's throughput tracking — no separate progress-tracking path.

    Total size is unknown (shows as "?", indeterminate bar) until the server's
    Content-Length header tells us otherwise — see set_total()/total_cb in
    download/engine.py. Not every server sends one, so it may never fire.
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[title]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
        )
        self._task_ids: dict[str, int] = {}

    def __enter__(self) -> ProgressReporter:
        self._progress.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        self._progress.__exit__(*exc_info)

    def register(self, job_id: str, title: str) -> None:
        self._task_ids[job_id] = self._progress.add_task("", title=title, total=None)

    def report(self, job_id: str, n: int) -> None:
        task_id = self._task_ids.get(job_id)
        if task_id is not None:
            self._progress.update(task_id, advance=n)

    def set_total(self, job_id: str, total: int) -> None:
        task_id = self._task_ids.get(job_id)
        if task_id is not None:
            self._progress.update(task_id, total=total)
