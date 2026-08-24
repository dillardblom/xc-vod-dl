from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Status = Literal["pending", "downloading", "verifying", "done", "failed", "skipped"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    series_id TEXT,
    season INTEGER,
    episode_num INTEGER,
    title TEXT NOT NULL,
    target_path TEXT NOT NULL,
    expected_size INTEGER,
    container_extension TEXT,
    status TEXT NOT NULL,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class DownloadRecord:
    id: str
    kind: str
    series_id: str | None
    season: int | None
    episode_num: int | None
    title: str
    target_path: str
    expected_size: int | None
    container_extension: str | None
    status: Status
    bytes_downloaded: int
    attempts: int
    last_error: str | None
    updated_at: str

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> DownloadRecord:
        # sqlite3.Row iterates its *values*, not its keys like a dict would —
        # .keys() is required here, not the redundant call ruff assumes it is.
        return cls(**{k: row[k] for k in row.keys()})  # noqa: SIM118


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Tracks per-item download status so a killed run can resume without
    re-browsing or re-querying the whole catalog.

    Safe to share across worker threads (parallel downloads all update the
    same store): the connection is opened with check_same_thread=False and
    every access is serialized through a single lock. Contention is a non-issue
    here — these are small, infrequent writes, not the hot path.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def upsert_pending(
        self,
        *,
        id: str,
        kind: str,
        title: str,
        target_path: str,
        series_id: str | None = None,
        season: int | None = None,
        episode_num: int | None = None,
        expected_size: int | None = None,
        container_extension: str | None = None,
    ) -> None:
        """Register an item as pending if it doesn't already exist. Existing
        records (including previously-failed ones, so retries are visible)
        are left untouched so status/progress from a prior run survives."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads (
                    id, kind, series_id, season, episode_num, title, target_path,
                    expected_size, container_extension, status, bytes_downloaded,
                    attempts, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    id,
                    kind,
                    series_id,
                    season,
                    episode_num,
                    title,
                    target_path,
                    expected_size,
                    container_extension,
                    _now(),
                ),
            )
            self._conn.commit()

    def mark_status(
        self,
        id: str,
        status: Status,
        *,
        bytes_downloaded: int | None = None,
        last_error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        params: list = [status, _now()]
        if bytes_downloaded is not None:
            fields.append("bytes_downloaded = ?")
            params.append(bytes_downloaded)
        if last_error is not None:
            fields.append("last_error = ?")
            params.append(last_error)
        if increment_attempts:
            fields.append("attempts = attempts + 1")
        params.append(id)
        with self._lock:
            self._conn.execute(f"UPDATE downloads SET {', '.join(fields)} WHERE id = ?", params)
            self._conn.commit()

    def get(self, id: str) -> DownloadRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM downloads WHERE id = ?", (id,)).fetchone()
        return DownloadRecord._from_row(row) if row else None

    def list_by_status(self, *statuses: Status) -> list[DownloadRecord]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM downloads WHERE status IN ({placeholders}) ORDER BY id", statuses
            ).fetchall()
        return [DownloadRecord._from_row(r) for r in rows]

    def list_incomplete(self) -> list[DownloadRecord]:
        return self.list_by_status("pending", "downloading", "verifying", "failed")

    def list_all(self) -> list[DownloadRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM downloads ORDER BY id").fetchall()
        return [DownloadRecord._from_row(r) for r in rows]
