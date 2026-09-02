from __future__ import annotations

from collections import defaultdict

from xc_vod_dl.api.models import Episode, SeriesInfo


def detect_gaps(episodes: list[Episode]) -> dict[int, list[int]]:
    """Find missing episode numbers within each season's observed range.

    Only flags episodes strictly between the lowest and highest episode_num
    seen for a season — a season starting at E01 with nothing after E08 is
    not "missing" E09+; it just hasn't aired/been added yet.
    """
    by_season: dict[int, set[int]] = defaultdict(set)
    for ep in episodes:
        by_season[ep.season].add(ep.episode_num)

    gaps: dict[int, list[int]] = {}
    for season, numbers in by_season.items():
        lo, hi = min(numbers), max(numbers)
        missing = [n for n in range(lo, hi + 1) if n not in numbers]
        if missing:
            gaps[season] = missing
    return dict(sorted(gaps.items()))


def detect_gaps_in_series(series: SeriesInfo) -> dict[int, list[int]]:
    """Convenience wrapper: run detect_gaps() over a SeriesInfo's episode dict."""
    all_episodes = [ep for eps in series.episodes.values() for ep in eps]
    return detect_gaps(all_episodes)


def detect_duplicate_episodes(episodes: list[Episode]) -> dict[int, list[int]]:
    """Find episode numbers listed more than once within a season — e.g. an
    upstream provider that double-listed one episode under two stream IDs.

    Reported separately from detect_gaps(): a duplicate doesn't mean content
    is actually missing (it inflates the episode count without leaving a
    hole), which is exactly what makes two same-named series listings with
    different total counts hard to tell apart otherwise — one may have a
    real gap, the other just a harmless duplicate.
    """
    by_season: dict[int, list[int]] = defaultdict(list)
    for ep in episodes:
        by_season[ep.season].append(ep.episode_num)

    dupes: dict[int, list[int]] = {}
    for season, numbers in by_season.items():
        seen: set[int] = set()
        repeated: list[int] = []
        for n in numbers:
            if n in seen and n not in repeated:
                repeated.append(n)
            seen.add(n)
        if repeated:
            dupes[season] = sorted(repeated)
    return dict(sorted(dupes.items()))


def detect_duplicate_episodes_in_series(series: SeriesInfo) -> dict[int, list[int]]:
    """Convenience wrapper: run detect_duplicate_episodes() over a SeriesInfo."""
    all_episodes = [ep for eps in series.episodes.values() for ep in eps]
    return detect_duplicate_episodes(all_episodes)


def detect_gaps_across_listings(
    episodes_by_listing: list[list[Episode]],
) -> list[dict[int, list[int]]]:
    """Compare multiple listings of (presumably) the same series and, for
    each one, find episode numbers present in at least one *other* listing
    but missing from this one.

    This catches what detect_gaps() structurally cannot: a missing
    *trailing* episode. A listing whose season 1 ends at E51 looks complete
    on its own — nothing implies E52 exists. Only a sibling listing that
    actually has E52 reveals the gap. Confirmed against a real catalog: a
    "Bluey" listing with S01 1-51 sitting right next to two other listings
    of the same show with S01 1-52 — detect_gaps() alone reports nothing
    wrong with any of them.

    Returns one gap map per input listing, same order as given.
    """
    universe: dict[int, set[int]] = defaultdict(set)
    per_listing: list[dict[int, set[int]]] = []
    for episodes in episodes_by_listing:
        by_season: dict[int, set[int]] = defaultdict(set)
        for ep in episodes:
            by_season[ep.season].add(ep.episode_num)
            universe[ep.season].add(ep.episode_num)
        per_listing.append(by_season)

    results = []
    for by_season in per_listing:
        missing: dict[int, list[int]] = {}
        for season, all_numbers in universe.items():
            gap = sorted(all_numbers - by_season.get(season, set()))
            if gap:
                missing[season] = gap
        results.append(dict(sorted(missing.items())))
    return results


def detect_gaps_across_series(infos: list[SeriesInfo]) -> list[dict[int, list[int]]]:
    """Convenience wrapper: run detect_gaps_across_listings() over multiple
    SeriesInfo objects (e.g. duplicate search results for the same show)."""
    return detect_gaps_across_listings(
        [[ep for eps in info.episodes.values() for ep in eps] for info in infos]
    )


def season_episode_counts(series: SeriesInfo) -> tuple[dict[int, int], dict[int, int]]:
    """present_counts, declared_counts for format_gap_report: how many
    episodes this listing actually has per season, and how many the
    provider's own season metadata claims there should be (0/absent if the
    provider doesn't populate that field — not every one does)."""
    present = {season: len(eps) for season, eps in series.episodes.items()}
    declared = {s.season_number: s.episode_count for s in series.seasons if s.episode_count}
    return present, declared


def format_gap_report(
    series_name: str,
    gaps: dict[int, list[int]],
    duplicates: dict[int, list[int]] | None = None,
    *,
    present_counts: dict[int, int] | None = None,
    declared_counts: dict[int, int] | None = None,
) -> str:
    """present_counts/declared_counts are optional (season -> count) maps
    used to annotate each incomplete season with "(present/expected)" —
    e.g. "Season 3: S03E26, ... (25/50)". Without them, a season reported
    as "missing 25 episodes" doesn't say whether that's a near-complete
    season or one that's barely started. declared_counts should come from
    the provider's own per-season episode_count when available; when it
    isn't (0/absent), the caller can omit it and this falls back to
    present + missing, which is at least a lower bound on the true total."""
    if not gaps and not duplicates:
        return f"{series_name}: no missing or duplicate episodes detected."
    lines = [f"{series_name}:"]
    if gaps:
        lines.append("  missing episodes:")
        for season, missing in gaps.items():
            missing_fmt = ", ".join(f"S{season:02d}E{n:02d}" for n in missing)
            count_note = ""
            if present_counts is not None:
                present = present_counts.get(season, 0)
                expected = (declared_counts or {}).get(season) or (present + len(missing))
                count_note = f"  ({present}/{expected})"
            lines.append(f"    Season {season}: {missing_fmt}{count_note}")
    if duplicates:
        lines.append("  duplicate episode numbers:")
        for season, repeated in duplicates.items():
            repeated_fmt = ", ".join(f"S{season:02d}E{n:02d}" for n in repeated)
            lines.append(f"    Season {season}: {repeated_fmt}")
    return "\n".join(lines)
