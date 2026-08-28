from xc_vod_dl.api.models import Episode
from xc_vod_dl.gaps import (
    detect_duplicate_episodes,
    detect_gaps,
    detect_gaps_across_listings,
    format_gap_report,
)


def make_episode(season: int, num: int) -> Episode:
    return Episode(
        episode_id=1000 + num,
        season=season,
        episode_num=num,
        title=f"E{num}",
        container_extension="mkv",
    )


def test_detect_gaps_finds_single_missing_episode():
    episodes = [make_episode(1, n) for n in [1, 2, 3, 4, 5, 6, 7, 8, 10]]
    gaps = detect_gaps(episodes)
    assert gaps == {1: [9]}


def test_detect_gaps_no_gap_when_contiguous():
    episodes = [make_episode(1, n) for n in [1, 2, 3]]
    assert detect_gaps(episodes) == {}


def test_detect_gaps_ignores_episodes_not_yet_aired_after_highest():
    # Only has E01-E03 so far; nothing implies E04+ is "missing".
    episodes = [make_episode(1, n) for n in [1, 2, 3]]
    assert detect_gaps(episodes) == {}


def test_detect_gaps_across_multiple_seasons():
    episodes = [make_episode(1, n) for n in [1, 2, 4]] + [make_episode(2, n) for n in [1, 3]]
    gaps = detect_gaps(episodes)
    assert gaps == {1: [3], 2: [2]}


def test_detect_gaps_multiple_missing_in_one_season():
    episodes = [make_episode(1, n) for n in [1, 5]]
    assert detect_gaps(episodes) == {1: [2, 3, 4]}


def test_format_gap_report_no_gaps():
    report = format_gap_report("My Show", {})
    assert "no missing" in report


def test_format_gap_report_with_gaps():
    report = format_gap_report("My Show", {1: [9]})
    assert "My Show" in report
    assert "S01E09" in report


def test_detect_duplicate_episodes_finds_a_repeated_number():
    episodes = [make_episode(1, n) for n in [1, 2, 2, 3]]
    assert detect_duplicate_episodes(episodes) == {1: [2]}


def test_detect_duplicate_episodes_none_when_all_unique():
    episodes = [make_episode(1, n) for n in [1, 2, 3]]
    assert detect_duplicate_episodes(episodes) == {}


def test_detect_duplicate_episodes_three_copies_reported_once():
    episodes = [make_episode(1, 1), make_episode(1, 1), make_episode(1, 1)]
    assert detect_duplicate_episodes(episodes) == {1: [1]}


def test_detect_duplicate_episodes_across_multiple_seasons():
    episodes = [make_episode(1, n) for n in [1, 1]] + [make_episode(2, n) for n in [5, 5]]
    assert detect_duplicate_episodes(episodes) == {1: [1], 2: [5]}


def test_format_gap_report_includes_duplicates():
    report = format_gap_report("My Show", {}, {1: [4]})
    assert "My Show" in report
    assert "S01E04" in report
    assert "duplicate" in report.lower()


def test_format_gap_report_no_gaps_or_duplicates():
    report = format_gap_report("My Show", {}, {})
    assert "no missing" in report.lower()


def test_detect_gaps_across_listings_finds_a_trailing_gap():
    """The exact real-world case that motivated this: one listing's season 1
    ends at E51, a sibling listing's ends at E52 — detect_gaps() alone can't
    tell E52 is missing from the first one (it just looks "not yet aired"),
    but comparing against the sibling reveals it."""
    complete = [make_episode(1, n) for n in range(1, 53)]  # 1..52
    truncated = [make_episode(1, n) for n in range(1, 52)]  # 1..51

    gaps_for_each = detect_gaps_across_listings([complete, truncated])

    assert gaps_for_each[0] == {}
    assert gaps_for_each[1] == {1: [52]}


def test_detect_gaps_across_listings_also_finds_interior_gaps():
    a = [make_episode(1, n) for n in [1, 2, 3]]
    b = [make_episode(1, n) for n in [1, 3]]

    gaps_for_each = detect_gaps_across_listings([a, b])

    assert gaps_for_each[0] == {}
    assert gaps_for_each[1] == {1: [2]}


def test_detect_gaps_across_listings_no_gaps_when_identical():
    a = [make_episode(1, n) for n in [1, 2, 3]]
    b = [make_episode(1, n) for n in [1, 2, 3]]

    assert detect_gaps_across_listings([a, b]) == [{}, {}]


def test_detect_gaps_across_listings_single_listing_is_a_noop():
    a = [make_episode(1, n) for n in [1, 2, 3]]
    assert detect_gaps_across_listings([a]) == [{}]
