from xc_vod_dl.api.models import Episode
from xc_vod_dl.gaps import detect_gaps, format_gap_report


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
    assert "no missing episodes" in report


def test_format_gap_report_with_gaps():
    report = format_gap_report("My Show", {1: [9]})
    assert "My Show" in report
    assert "S01E09" in report
