from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from xc_vod_dl.exceptions import ConfigError

ENV_SERVER = "XCVODDL_SERVER"
ENV_USERNAME = "XCVODDL_USERNAME"
ENV_PASSWORD = "XCVODDL_PASSWORD"


def default_config_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "xc-vod-dl" / "config.toml"


def _resolve_default_config_path() -> Path:
    """A `config.toml` in the current directory takes precedence over the
    XDG user config, so `cp config.example.toml config.toml` inside a
    project/download directory works without needing `--config` every time."""
    local = Path("config.toml")
    if local.is_file():
        return local
    return default_config_path()


@dataclass(frozen=True)
class AccountConfig:
    server: str
    username: str
    password: str


@dataclass(frozen=True)
class DownloadConfig:
    movies_dir: Path = Path("Movies")
    series_dir: Path = Path("Series")
    verify_mode: str = "quick"
    download_nfo: bool = True
    download_cover: bool = True
    logfile: Path = Path("voddl.log")


@dataclass(frozen=True)
class ConcurrencyConfig:
    max_parallel_ceiling: int = 4
    safety_margin: int = 1
    cooldown_s: float = 30.0
    recovery_streak: int = 3
    collapse_floor_pct: float = 0.2


@dataclass(frozen=True)
class Config:
    account: AccountConfig
    download: DownloadConfig = field(default_factory=DownloadConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(
    config_path: Path | None = None,
    *,
    server: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> Config:
    """Resolve configuration in priority order: CLI flags > env vars > config file.

    Raises ConfigError if credentials cannot be resolved from any source.
    """
    path = config_path or _resolve_default_config_path()
    raw = _load_toml(path)

    raw_account = raw.get("account", {})
    resolved_server = server or os.environ.get(ENV_SERVER) or raw_account.get("server")
    resolved_username = username or os.environ.get(ENV_USERNAME) or raw_account.get("username")
    resolved_password = password or os.environ.get(ENV_PASSWORD) or raw_account.get("password")

    missing = [
        name
        for name, value in (
            ("server", resolved_server),
            ("username", resolved_username),
            ("password", resolved_password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "missing required credential(s): "
            + ", ".join(missing)
            + f". Set via CLI flags, {ENV_SERVER}/{ENV_USERNAME}/{ENV_PASSWORD} env vars, "
            + f"or {path}"
        )

    account = AccountConfig(
        server=resolved_server, username=resolved_username, password=resolved_password
    )

    raw_download = raw.get("download", {})
    download = DownloadConfig(
        movies_dir=Path(raw_download.get("movies_dir", "Movies")),
        series_dir=Path(raw_download.get("series_dir", "Series")),
        verify_mode=raw_download.get("verify_mode", "quick"),
        download_nfo=raw_download.get("download_nfo", True),
        download_cover=raw_download.get("download_cover", True),
        logfile=Path(raw_download.get("logfile", "voddl.log")),
    )

    raw_concurrency = raw.get("concurrency", {})
    concurrency = ConcurrencyConfig(
        max_parallel_ceiling=raw_concurrency.get("max_parallel_ceiling", 4),
        safety_margin=raw_concurrency.get("safety_margin", 1),
        cooldown_s=raw_concurrency.get("cooldown_s", 30.0),
        recovery_streak=raw_concurrency.get("recovery_streak", 3),
        collapse_floor_pct=raw_concurrency.get("collapse_floor_pct", 0.2),
    )

    return Config(account=account, download=download, concurrency=concurrency)
