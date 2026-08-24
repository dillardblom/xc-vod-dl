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


def format_gap_report(series_name: str, gaps: dict[int, list[int]]) -> str:
    if not gaps:
        return f"{series_name}: no missing episodes detected."
    lines = [f"{series_name}: missing episodes detected"]
    for season, missing in gaps.items():
        missing_fmt = ", ".join(f"S{season:02d}E{n:02d}" for n in missing)
        lines.append(f"  Season {season}: {missing_fmt}")
    return "\n".join(lines)
