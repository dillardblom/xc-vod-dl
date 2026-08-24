from __future__ import annotations

import requests

from xc_vod_dl.api.models import (
    Account,
    Category,
    SeriesInfo,
    SeriesStream,
    VodInfo,
    VodStream,
)
from xc_vod_dl.exceptions import AuthError, XtreamAPIError


class XtreamClient:
    """Thin wrapper around an Xtream Codes `player_api.php` endpoint.

    Each instance owns its own requests.Session — when used from multiple
    worker threads, create one client per thread rather than sharing one.
    """

    def __init__(self, server: str, username: str, password: str, timeout: float = 15.0):
        self.server = server.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, params: dict | None = None) -> dict | list:
        base_params = {"username": self.username, "password": self.password}
        if params:
            base_params.update(params)
        try:
            resp = self.session.get(
                f"{self.server}/player_api.php", params=base_params, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise XtreamAPIError(f"request to player_api.php failed: {exc}") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise XtreamAPIError("player_api.php did not return valid JSON") from exc

    def get_account(self) -> Account:
        data = self._get()
        if not isinstance(data, dict) or "user_info" not in data:
            raise XtreamAPIError("unexpected response shape for account info")
        account = Account.from_json(data)
        if account.status not in ("Active", "active"):
            raise AuthError(f"account status is '{account.status}', not Active")
        return account

    def get_vod_categories(self) -> list[Category]:
        data = self._get({"action": "get_vod_categories"})
        return [Category.from_json(c) for c in _as_list(data)]

    def get_series_categories(self) -> list[Category]:
        data = self._get({"action": "get_series_categories"})
        return [Category.from_json(c) for c in _as_list(data)]

    def get_vod_streams(self, category_id: str | None = None) -> list[VodStream]:
        params = {"action": "get_vod_streams"}
        if category_id is not None:
            params["category_id"] = category_id
        data = self._get(params)
        return [VodStream.from_json(v) for v in _as_list(data)]

    def get_series_streams(self, category_id: str | None = None) -> list[SeriesStream]:
        params = {"action": "get_series_streams"}
        if category_id is not None:
            params["category_id"] = category_id
        data = self._get(params)
        return [SeriesStream.from_json(s) for s in _as_list(data)]

    def get_series_info(self, series_id: int) -> SeriesInfo:
        data = self._get({"action": "get_series_info", "series_id": series_id})
        if not isinstance(data, dict):
            raise XtreamAPIError(f"unexpected response shape for series {series_id}")
        return SeriesInfo.from_json(data)

    def get_vod_info(self, vod_id: int) -> VodInfo:
        data = self._get({"action": "get_vod_info", "vod_id": vod_id})
        if not isinstance(data, dict):
            raise XtreamAPIError(f"unexpected response shape for vod {vod_id}")
        return VodInfo.from_json(data)

    def movie_url(self, stream_id: int, container_extension: str) -> str:
        return (
            f"{self.server}/movie/{self.username}/{self.password}/"
            f"{stream_id}.{container_extension}"
        )

    def episode_url(self, episode_id: int, container_extension: str) -> str:
        return (
            f"{self.server}/series/{self.username}/{self.password}/"
            f"{episode_id}.{container_extension}"
        )


def _as_list(data: dict | list) -> list:
    # Some Xtream panels return {} instead of [] for an empty category.
    if isinstance(data, dict) and not data:
        return []
    if not isinstance(data, list):
        raise XtreamAPIError(f"expected a list response, got {type(data).__name__}")
    return data
