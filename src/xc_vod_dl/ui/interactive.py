from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import questionary
from rich.console import Console

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.api.models import Category, SeriesInfo, SeriesStream, VodStream
from xc_vod_dl.exceptions import XcVodDlError
from xc_vod_dl.gaps import (
    detect_duplicate_episodes_in_series,
    detect_gaps_across_series,
    detect_gaps_in_series,
    format_gap_report,
)
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


def _prompt_rename(original_name: str) -> str | None:
    """Optional post-selection rename. Most valuable for a series, where a
    single upstream naming quirk (e.g. a leading language-code prefix) would
    otherwise mean manually renaming every episode file and its .nfo one by
    one after the fact. Returns the name to use for folders/filenames/.nfo
    (unchanged if kept, or the typed replacement) — never None once
    reached; a cancelled prompt (Ctrl-C/Esc) also falls back to the original.
    """
    keep = questionary.confirm(f"Keep name '{original_name}' as-is?", default=True).ask()
    if keep is None or keep:
        return original_name
    new_name = questionary.text("New name:", default=original_name).ask()
    return new_name if new_name else original_name


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

    # Format/year are free (already in the streams listing). Duration needs
    # a get_vod_info() call per result — checked against three real servers:
    # always empty on one, populated on the other two — so it's fetched the
    # same way series search already pays for season/episode counts, rather
    # than skipped outright.
    duration_by_id = _fetch_movie_duration_with_progress(client, matches)

    choices = [
        questionary.Choice(
            title=_movie_label(
                s, cat_names.get(s.category_id, s.category_id), duration_by_id.get(s.stream_id)
            ),
            value=s,
        )
        for s in matches
    ]
    selected = questionary.checkbox(
        "Select movie(s) (space to toggle, enter to confirm):", choices=choices
    ).ask()
    if not selected:
        return []
    return _movie_specs_with_rename(selected)


def _fetch_movie_duration_with_progress(
    client: XtreamClient, matches: list[VodStream]
) -> dict[int, str | None]:
    results: dict[int, str | None] = {}
    with console.status(f"Fetching details for {len(matches)} movie(s)...") as status:
        for i, s in enumerate(matches, start=1):
            status.update(f"Fetching details for movie {i}/{len(matches)}...")
            try:
                results[s.stream_id] = client.get_vod_info(s.stream_id).duration
            except XcVodDlError:
                results[s.stream_id] = None
    return results


def _movie_label(s: VodStream, category: str | None = None, duration: str | None = None) -> str:
    """Resolution/audio-language aren't in the picture: checked against three
    real servers, the video/audio sub-objects are never populated for movies
    on any of them (only sometimes for series/episodes) — not worth fetching
    for something that's never actually there. Duration is, on two of three."""
    label = s.name
    if category:
        label += f"  [{category}]"
    extra = s.container_extension.upper()
    if s.year:
        extra += f", {s.year}"
    if duration:
        extra += f", {duration}"
    return f"{label}  ({extra})"


def _movie_specs_with_rename(selected: list[VodStream]) -> list[JobSpec]:
    specs = []
    for s in selected:
        name = _prompt_rename(s.name)
        specs.append(
            JobSpec(kind="movie", id=s.stream_id, display_name=name if name != s.name else None)
        )
    return specs


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

    # Duplicate-name results (different upstream providers) are exactly the
    # case where you need to know *which* episode a copy is missing/repeats
    # before picking one — not just that it has "[gaps]" somewhere — since
    # the fix might be grabbing one specific episode from a different copy
    # rather than the whole thing. Cross-checked against sibling listings
    # where possible: a single listing can't tell "season ends at E51" from
    # "E52 hasn't aired yet", but a sibling listing that does have E52 can.
    cross_gap_by_id = _cross_series_gap_maps(matches, info_by_id)

    choices = []
    for s in matches:
        label = f"{s.name}  [{cat_names.get(s.category_id, s.category_id)}]"
        info = info_by_id.get(s.series_id)
        if info is not None:
            n_seasons = len(info.episodes)
            n_episodes = sum(len(eps) for eps in info.episodes.values())
            label += f"  ({n_seasons} season(s), {n_episodes} episode(s))"
            resolution = _sample_resolution(info)
            if resolution:
                label += f"  [{resolution}]"
            gap_map = cross_gap_by_id.get(s.series_id) or detect_gaps_in_series(info)
            dupe_map = detect_duplicate_episodes_in_series(info)
            if gap_map or dupe_map:
                label += "  [gaps]" if gap_map else "  [duplicates]"
                console.print(f"[yellow]{format_gap_report(label.strip(), gap_map, dupe_map)}[/yellow]")
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
        specs.extend(
            _pick_series_scope(
                client,
                series_stream,
                info_by_id.get(series_stream.series_id),
                cross_gap_by_id.get(series_stream.series_id),
            )
        )
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


def _sample_resolution(info: SeriesInfo) -> str | None:
    """Episode-level resolution is only sometimes populated by the backend
    (checked against a real catalog: roughly half of series had it, the
    rest didn't at all) and duration isn't a useful signal at the series
    level (it varies episode to episode by design). Resolution is the one
    field worth surfacing per search result — take the first episode that
    actually has it as a representative sample for the whole listing."""
    for eps in info.episodes.values():
        for ep in eps:
            if ep.resolution:
                return ep.resolution
    return None


def _cross_series_gap_maps(
    matches: list[SeriesStream], info_by_id: dict[int, SeriesInfo | None]
) -> dict[int, dict[int, list[int]]]:
    """Groups search results by their exact set of season numbers (a cheap,
    reliable proxy for "these are duplicate listings of the same show" —
    confirmed against a real catalog: a show's main listings all share
    {1, 2, 3} while an unrelated spin-off with only a season 1 naturally
    falls into its own group of one, so it's never wrongly compared against
    a completely different episode count) and cross-checks each member
    against the others via detect_gaps_across_series()."""
    groups: dict[frozenset[int], list[int]] = {}
    for s in matches:
        info = info_by_id.get(s.series_id)
        if info is None or not info.episodes:
            continue
        groups.setdefault(frozenset(info.episodes), []).append(s.series_id)

    result: dict[int, dict[int, list[int]]] = {}
    for series_ids in groups.values():
        if len(series_ids) < 2:
            continue
        infos = [info_by_id[sid] for sid in series_ids]
        for sid, gap_map in zip(series_ids, detect_gaps_across_series(infos)):
            if gap_map:
                result[sid] = gap_map
    return result


def _browse_movies_by_category(client: XtreamClient) -> list[JobSpec]:
    category = _pick_category(client.get_vod_categories())
    if category is None:
        return []

    streams = client.get_vod_streams(category_id=category.category_id)
    if not streams:
        console.print("[yellow]No movies in this category.[/yellow]")
        return []

    choices = [questionary.Choice(title=_movie_label(s), value=s) for s in streams]
    selected = questionary.checkbox(
        "Select movie(s) (space to toggle, enter to confirm):", choices=choices
    ).ask()
    if not selected:
        return []
    return _movie_specs_with_rename(selected)


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
    client: XtreamClient,
    series_stream: SeriesStream,
    info: SeriesInfo | None = None,
    known_gap_map: dict[int, list[int]] | None = None,
) -> list[JobSpec]:
    if info is None:
        try:
            info = client.get_series_info(series_stream.series_id)
        except XcVodDlError as exc:
            console.print(f"[red]Could not load '{series_stream.name}': {exc}[/red]")
            return []
    # known_gap_map, when given, comes from cross-checking this listing
    # against sibling search results and can see gaps a single listing
    # can't (e.g. a missing trailing episode) — prefer it when available.
    gap_map = known_gap_map or detect_gaps_in_series(info)
    dupe_map = detect_duplicate_episodes_in_series(info)
    if gap_map or dupe_map:
        console.print(f"[yellow]{format_gap_report(info.name, gap_map, dupe_map)}[/yellow]")

    name = _prompt_rename(info.name)
    display_name = name if name != info.name else None

    scope = questionary.select(
        f"Download from '{name}':",
        choices=["Whole series", "One or more seasons", "A specific episode"],
    ).ask()
    if scope is None:
        return []
    if scope == "Whole series":
        return [JobSpec(kind="series", id=series_stream.series_id, display_name=display_name)]

    if scope == "One or more seasons":
        season_choices = questionary.checkbox(
            "Season(s):",
            choices=[questionary.Choice(str(s), value=s) for s in sorted(info.episodes)],
        ).ask()
        if not season_choices:
            return []
        return [
            JobSpec(kind="series", id=series_stream.series_id, season=s, display_name=display_name)
            for s in season_choices
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
            kind="series",
            id=series_stream.series_id,
            season=season_num,
            episode=int(episode_num),
            display_name=display_name,
        )
    ]
