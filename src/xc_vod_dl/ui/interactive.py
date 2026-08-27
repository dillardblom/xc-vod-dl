from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import questionary
from rich.console import Console

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.api.models import Category, SeriesInfo, SeriesStream
from xc_vod_dl.exceptions import XcVodDlError
from xc_vod_dl.gaps import detect_gaps_in_series, format_gap_report
from xc_vod_dl.jobs import JobSpec

console = Console()

_MAX_SEARCH_RESULTS = 40

T = TypeVar("T")


def browse_and_select(client: XtreamClient) -> list[JobSpec]:
    """Top-level interactive loop: Movies/Series -> search-by-name or
    category -> item(s) -> (for category-browsed series) whole/season/episode
    scope. Returns everything the user picked across as many round trips
    through the menu as they like.

    Catalog/category fetches are cached for the life of this call (not just
    per-search), since the catalog is large (tens of thousands of items) and
    doesn't change mid-session."""
    cache: dict[str, list] = {}
    specs: list[JobSpec] = []
    while True:
        choice = questionary.select(
            "What do you want to browse?", choices=["Movies", "Series", "Done"]
        ).ask()
        if choice is None or choice == "Done":
            break
        if choice == "Movies":
            specs.extend(_handle_movies(client, cache))
        else:
            specs.extend(_handle_series(client, cache))
    return specs


def _cached(cache: dict[str, list], key: str, fetch: Callable[[], list]) -> list:
    if key not in cache:
        cache[key] = fetch()
    return cache[key]


def _pick_category(categories: list[Category]) -> Category | None:
    if not categories:
        console.print("[yellow]No categories found.[/yellow]")
        return None
    choices = [questionary.Choice(title=c.category_name, value=c) for c in categories]
    return questionary.select("Category:", choices=choices).ask()


def _handle_movies(client: XtreamClient, cache: dict[str, list]) -> list[JobSpec]:
    mode = questionary.select("Movies:", choices=["Search by name", "Browse by category"]).ask()
    if mode is None:
        return []
    if mode == "Search by name":
        return _search_movies(client, cache)
    return _browse_movies_by_category(client)


def _handle_series(client: XtreamClient, cache: dict[str, list]) -> list[JobSpec]:
    mode = questionary.select("Series:", choices=["Search by name", "Browse by category"]).ask()
    if mode is None:
        return []
    if mode == "Search by name":
        return _search_series(client, cache)
    return _browse_series_by_category(client)


def _find_matches(
    query: str,
    fetch_items: Callable[[], list[T]],
    fetch_categories: Callable[[], list[Category]],
    name_of: Callable[[T], str],
    status_label: str,
) -> tuple[list[T], dict[str, str]]:
    """Fetches the catalog + categories under a spinner (the full catalog can
    be tens of thousands of items and take several seconds) and filters by
    case-insensitive substring match on name. Cross-category by design:
    search spans the whole catalog for the given type, since which category
    something landed in isn't always obvious."""
    with console.status(status_label):
        items = fetch_items()
        cat_names = {c.category_id: c.category_name for c in fetch_categories()}

    query_lower = query.lower()
    matches = [item for item in items if query_lower in name_of(item).lower()]
    if len(matches) > _MAX_SEARCH_RESULTS:
        console.print(
            f"[dim]{len(matches)} matches for '{query}' — showing the first "
            f"{_MAX_SEARCH_RESULTS}. Narrow your search for more precise results.[/dim]"
        )
        matches = matches[:_MAX_SEARCH_RESULTS]
    return matches, cat_names


def _search_movies(client: XtreamClient, cache: dict[str, list]) -> list[JobSpec]:
    query = questionary.text("Search movies:").ask()
    if not query:
        return []

    matches, cat_names = _find_matches(
        query,
        lambda: _cached(cache, "vod_all", client.get_vod_streams),
        lambda: _cached(cache, "vod_categories", client.get_vod_categories),
        lambda s: s.name,
        "Searching movies...",
    )
    if not matches:
        console.print(f"[yellow]No results matching '{query}'.[/yellow]")
        return []

    choices = [
        questionary.Choice(
            title=f"{s.name}  [{cat_names.get(s.category_id, s.category_id)}]", value=s
        )
        for s in matches
    ]
    selected = questionary.checkbox(
        "Select movie(s) (space to toggle, enter to confirm):", choices=choices
    ).ask()
    if not selected:
        return []
    return [JobSpec(kind="movie", id=s.stream_id) for s in selected]


def _search_series(client: XtreamClient, cache: dict[str, list]) -> list[JobSpec]:
    query = questionary.text("Search series:").ask()
    if not query:
        return []

    matches, cat_names = _find_matches(
        query,
        lambda: _cached(cache, "series_all", client.get_series_streams),
        lambda: _cached(cache, "series_categories", client.get_series_categories),
        lambda s: s.name,
        "Searching series...",
    )
    if not matches:
        console.print(f"[yellow]No results matching '{query}'.[/yellow]")
        return []

    # Same series often appears more than once — once per upstream provider —
    # and one copy can be missing a season the other has. Season/episode
    # counts make that visible before downloading, not after.
    info_by_id = _fetch_series_info_with_progress(client, matches)

    choices = []
    for s in matches:
        label = f"{s.name}  [{cat_names.get(s.category_id, s.category_id)}]"
        info = info_by_id.get(s.series_id)
        if info is not None:
            n_seasons = len(info.episodes)
            n_episodes = sum(len(eps) for eps in info.episodes.values())
            label += f"  ({n_seasons} season(s), {n_episodes} episode(s))"
            if detect_gaps_in_series(info):
                label += "  [gaps]"
        else:
            label += "  (season info unavailable)"
        choices.append(questionary.Choice(title=label, value=s))

    selected: list[SeriesStream] = (
        questionary.checkbox(
            "Select series (space to toggle, enter to confirm):", choices=choices
        ).ask()
        or []
    )
    specs: list[JobSpec] = []
    for series_stream in selected:
        specs.extend(_pick_series_scope(client, series_stream, info_by_id.get(series_stream.series_id)))
    return specs


def _fetch_series_info_with_progress(
    client: XtreamClient, matches: list[SeriesStream]
) -> dict[int, SeriesInfo | None]:
    results: dict[int, SeriesInfo | None] = {}
    with console.status(f"Fetching season info for {len(matches)} series...") as status:
        for i, s in enumerate(matches, start=1):
            status.update(f"Fetching season info for series {i}/{len(matches)}...")
            try:
                results[s.series_id] = client.get_series_info(s.series_id)
            except XcVodDlError:
                results[s.series_id] = None
    return results


def _browse_movies_by_category(client: XtreamClient) -> list[JobSpec]:
    category = _pick_category(client.get_vod_categories())
    if category is None:
        return []

    streams = client.get_vod_streams(category_id=category.category_id)
    if not streams:
        console.print("[yellow]No movies in this category.[/yellow]")
        return []

    choices = [questionary.Choice(title=s.name, value=s) for s in streams]
    selected = questionary.checkbox(
        "Select movie(s) (space to toggle, enter to confirm):", choices=choices
    ).ask()
    if not selected:
        return []
    return [JobSpec(kind="movie", id=s.stream_id) for s in selected]


def _browse_series_by_category(client: XtreamClient) -> list[JobSpec]:
    category = _pick_category(client.get_series_categories())
    if category is None:
        return []

    streams = client.get_series_streams(category_id=category.category_id)
    if not streams:
        console.print("[yellow]No series in this category.[/yellow]")
        return []

    choices = [questionary.Choice(title=s.name, value=s) for s in streams]
    series_stream: SeriesStream | None = questionary.select("Series:", choices=choices).ask()
    if series_stream is None:
        return []
    return _pick_series_scope(client, series_stream)


def _pick_series_scope(
    client: XtreamClient, series_stream: SeriesStream, info: SeriesInfo | None = None
) -> list[JobSpec]:
    if info is None:
        try:
            info = client.get_series_info(series_stream.series_id)
        except XcVodDlError as exc:
            console.print(f"[red]Could not load '{series_stream.name}': {exc}[/red]")
            return []
    gap_map = detect_gaps_in_series(info)
    if gap_map:
        console.print(f"[yellow]{format_gap_report(info.name, gap_map)}[/yellow]")

    scope = questionary.select(
        f"Download from '{info.name}':",
        choices=["Whole series", "One or more seasons", "A specific episode"],
    ).ask()
    if scope is None:
        return []
    if scope == "Whole series":
        return [JobSpec(kind="series", id=series_stream.series_id)]

    if scope == "One or more seasons":
        season_choices = questionary.checkbox(
            "Season(s):",
            choices=[questionary.Choice(str(s), value=s) for s in sorted(info.episodes)],
        ).ask()
        if not season_choices:
            return []
        return [
            JobSpec(kind="series", id=series_stream.series_id, season=s) for s in season_choices
        ]

    season_num = questionary.select(
        "Season:", choices=[str(s) for s in sorted(info.episodes)]
    ).ask()
    if season_num is None:
        return []
    season_num = int(season_num)

    episode_num = questionary.select(
        "Episode:",
        choices=[
            str(e.episode_num) for e in sorted(info.episodes[season_num], key=lambda e: e.episode_num)
        ],
    ).ask()
    if episode_num is None:
        return []
    return [
        JobSpec(
            kind="series", id=series_stream.series_id, season=season_num, episode=int(episode_num)
        )
    ]
