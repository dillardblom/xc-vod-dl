from __future__ import annotations

from dataclasses import dataclass, field


def _int(value, default: int = 0) -> int:
    """Xtream servers inconsistently return numeric fields as int, str, or null."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Account:
    username: str
    status: str
    max_connections: int
    active_cons: int
    is_trial: bool
    exp_date: str | None

    @classmethod
    def from_json(cls, data: dict) -> Account:
        info = data["user_info"]
        return cls(
            username=info.get("username", ""),
            status=info.get("status", ""),
            max_connections=_int(info.get("max_connections"), default=1),
            active_cons=_int(info.get("active_cons"), default=0),
            is_trial=str(info.get("is_trial", "0")) == "1",
            exp_date=info.get("exp_date"),
        )


@dataclass(frozen=True)
class Category:
    category_id: str
    category_name: str

    @classmethod
    def from_json(cls, data: dict) -> Category:
        return cls(
            category_id=str(data.get("category_id", "")),
            category_name=data.get("category_name", ""),
        )


@dataclass(frozen=True)
class VodStream:
    stream_id: int
    name: str
    category_id: str
    container_extension: str
    year: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> VodStream:
        year = data.get("year")
        return cls(
            stream_id=_int(data.get("stream_id")),
            name=data.get("name", ""),
            category_id=str(data.get("category_id", "")),
            container_extension=data.get("container_extension") or "mp4",
            year=str(year) if year else None,
        )


@dataclass(frozen=True)
class SeriesStream:
    series_id: int
    name: str
    category_id: str
    plot: str = ""
    genre: str = ""
    release_date: str = ""

    @classmethod
    def from_json(cls, data: dict) -> SeriesStream:
        return cls(
            series_id=_int(data.get("series_id")),
            name=data.get("name", ""),
            category_id=str(data.get("category_id", "")),
            plot=data.get("plot", "") or "",
            genre=data.get("genre", "") or "",
            release_date=data.get("releaseDate", "") or data.get("release_date", "") or "",
        )


@dataclass(frozen=True)
class Episode:
    episode_id: int
    season: int
    episode_num: int
    title: str
    container_extension: str
    plot: str = ""
    tmdb_id: str | None = None
    resolution: str | None = None

    @classmethod
    def from_json(cls, data: dict, season: int) -> Episode:
        info = data.get("info") or {}
        video = info.get("video") or {}
        width, height = video.get("width"), video.get("height")
        return cls(
            episode_id=_int(data.get("id")),
            season=_int(data.get("season"), default=season) or season,
            episode_num=_int(data.get("episode_num")),
            title=data.get("title", "") or "",
            container_extension=data.get("container_extension") or "mp4",
            plot=info.get("plot", "") or "",
            tmdb_id=str(info["tmdb_id"]) if info.get("tmdb_id") else None,
            resolution=f"{width}x{height}" if width and height else None,
        )


@dataclass(frozen=True)
class Season:
    season_number: int
    name: str
    episode_count: int

    @classmethod
    def from_json(cls, data: dict) -> Season:
        return cls(
            season_number=_int(data.get("season_number")),
            name=data.get("name", "") or "",
            episode_count=_int(data.get("episode_count")),
        )


@dataclass(frozen=True)
class SeriesInfo:
    name: str
    plot: str
    genre: str
    tmdb_id: str | None
    seasons: list[Season]
    episodes: dict[int, list[Episode]] = field(default_factory=dict)
    cover: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> SeriesInfo:
        info = data.get("info") or {}
        seasons = [Season.from_json(s) for s in data.get("seasons") or []]
        episodes: dict[int, list[Episode]] = {}
        for season_key, ep_list in (data.get("episodes") or {}).items():
            season_num = _int(season_key)
            episodes[season_num] = [Episode.from_json(e, season_num) for e in ep_list]
        return cls(
            name=info.get("name", "") or "",
            plot=info.get("plot", "") or "",
            genre=info.get("genre", "") or "",
            tmdb_id=str(info["tmdb_id"]) if info.get("tmdb_id") else None,
            seasons=seasons,
            episodes=episodes,
            cover=info.get("cover") or None,
        )


@dataclass(frozen=True)
class VodInfo:
    name: str
    plot: str
    genre: str
    release_date: str
    tmdb_id: str | None
    stream_id: int
    container_extension: str
    cover: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> VodInfo:
        info = data.get("info") or {}
        movie_data = data.get("movie_data") or {}
        return cls(
            name=movie_data.get("name", "") or info.get("name", "") or "",
            plot=info.get("plot", "") or info.get("description", "") or "",
            genre=info.get("genre", "") or "",
            release_date=info.get("releasedate", "") or "",
            tmdb_id=str(info["tmdb_id"]) if info.get("tmdb_id") else None,
            stream_id=_int(movie_data.get("stream_id")),
            container_extension=movie_data.get("container_extension") or "mp4",
            cover=info.get("movie_image") or info.get("cover_big") or None,
        )
