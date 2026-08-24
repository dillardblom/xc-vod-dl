from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["movie", "series"]


@dataclass(frozen=True)
class JobSpec:
    """One line from a manifest file (or one interactive selection), before
    it's been resolved against the API into concrete DownloadJob(s).

    `movie:12345`        -> a single movie
    `series:6789`         -> a whole series, all seasons
    `series:6789:2`       -> season 2 only
    `series:6789:2:5`     -> a single episode
    """

    kind: Kind
    id: int
    season: int | None = None
    episode: int | None = None


def parse_manifest_line(line: str) -> JobSpec | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return None

    parts = stripped.split(":")
    kind = parts[0].strip().lower()
    if kind not in ("movie", "series"):
        raise ValueError(f"unknown manifest entry kind {kind!r} in line: {line!r}")

    if kind == "movie":
        if len(parts) != 2:
            raise ValueError(f"movie entries take exactly one id, got: {line!r}")
        return JobSpec(kind="movie", id=_parse_int(parts[1], line))

    # series
    if len(parts) not in (2, 3, 4):
        raise ValueError(f"malformed series entry: {line!r}")
    series_id = _parse_int(parts[1], line)
    season = _parse_int(parts[2], line) if len(parts) >= 3 else None
    episode = _parse_int(parts[3], line) if len(parts) == 4 else None
    return JobSpec(kind="series", id=series_id, season=season, episode=episode)


def _parse_int(value: str, line: str) -> int:
    value = value.strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"expected an integer id, got {value!r} in line: {line!r}") from exc


def parse_manifest(path: Path) -> list[JobSpec]:
    specs = []
    for line in path.read_text().splitlines():
        spec = parse_manifest_line(line)
        if spec is not None:
            specs.append(spec)
    return specs
