from __future__ import annotations

import questionary
from rich.console import Console

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.gaps import detect_gaps_in_series, format_gap_report
from xc_vod_dl.jobs import JobSpec

console = Console()


def browse_and_select(client: XtreamClient) -> list[JobSpec]:
    """Top-level interactive loop: Movies/Series -> category -> item(s) ->
    (for series) whole/season/episode scope. Returns everything the user
    picked across as many round trips through the menu as they like."""
    specs: list[JobSpec] = []
    while True:
        choice = questionary.select(
            "What do you want to browse?", choices=["Movies", "Series", "Done"]
        ).ask()
        if choice is None or choice == "Done":
            break
        if choice == "Movies":
            specs.extend(_browse_movies(client))
        else:
            specs.extend(_browse_series(client))
    return specs


def _pick_category(client: XtreamClient, get_categories):
    categories = get_categories()
    if not categories:
        console.print("[yellow]No categories found.[/yellow]")
        return None
    name = questionary.select("Category:", choices=[c.category_name for c in categories]).ask()
    if name is None:
        return None
    return next(c for c in categories if c.category_name == name)


def _browse_movies(client: XtreamClient) -> list[JobSpec]:
    category = _pick_category(client, client.get_vod_categories)
    if category is None:
        return []

    streams = client.get_vod_streams(category_id=category.category_id)
    if not streams:
        console.print("[yellow]No movies in this category.[/yellow]")
        return []

    selected_names = questionary.checkbox(
        "Select movie(s) (space to toggle, enter to confirm):", choices=[s.name for s in streams]
    ).ask()
    if not selected_names:
        return []

    by_name = {s.name: s for s in streams}
    return [JobSpec(kind="movie", id=by_name[name].stream_id) for name in selected_names]


def _browse_series(client: XtreamClient) -> list[JobSpec]:
    category = _pick_category(client, client.get_series_categories)
    if category is None:
        return []

    streams = client.get_series_streams(category_id=category.category_id)
    if not streams:
        console.print("[yellow]No series in this category.[/yellow]")
        return []

    series_name = questionary.select("Series:", choices=[s.name for s in streams]).ask()
    if series_name is None:
        return []
    series_stream = next(s for s in streams if s.name == series_name)

    info = client.get_series_info(series_stream.series_id)
    gap_map = detect_gaps_in_series(info)
    if gap_map:
        console.print(f"[yellow]{format_gap_report(info.name, gap_map)}[/yellow]")

    scope = questionary.select(
        f"Download from '{info.name}':",
        choices=["Whole series", "A specific season", "A specific episode"],
    ).ask()
    if scope is None:
        return []
    if scope == "Whole series":
        return [JobSpec(kind="series", id=series_stream.series_id)]

    season_num = questionary.select(
        "Season:", choices=[str(s) for s in sorted(info.episodes)]
    ).ask()
    if season_num is None:
        return []
    season_num = int(season_num)
    if scope == "A specific season":
        return [JobSpec(kind="series", id=series_stream.series_id, season=season_num)]

    episode_num = questionary.select(
        "Episode:",
        choices=[str(e.episode_num) for e in sorted(info.episodes[season_num], key=lambda e: e.episode_num)],
    ).ask()
    if episode_num is None:
        return []
    return [
        JobSpec(
            kind="series", id=series_stream.series_id, season=season_num, episode=int(episode_num)
        )
    ]
