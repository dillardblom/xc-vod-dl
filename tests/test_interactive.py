from unittest.mock import MagicMock

import pytest
import questionary

from xc_vod_dl.api.models import Category, Episode, SeriesInfo, SeriesStream, VodStream
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.ui import interactive as interactive_module


class FakeQuestion:
    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


@pytest.fixture(autouse=True)
def _default_keep_name_as_is(monkeypatch):
    """questionary.confirm now backs the optional post-selection rename
    prompt — default every test to "keep the name as-is" so existing
    selection-flow tests don't need to know about it. Tests that actually
    exercise renaming override this locally via monkeypatch.setattr."""
    monkeypatch.setattr(
        interactive_module.questionary, "confirm", lambda *args, **kwargs: FakeQuestion(True)
    )


def _title_and_value(choice):
    # Plain strings have a `.title` *method* (str.title()), so hasattr(choice,
    # "title") is true for them too — must check the actual Choice type.
    if isinstance(choice, questionary.Choice):
        return choice.title, choice.value
    return choice, choice


def _select_sequence(monkeypatch, answers):
    """Mocks questionary.select to resolve each queued answer by matching it
    against the *title* of the choices passed in, returning the choice's
    underlying value — mirrors real questionary.Choice semantics closely
    enough to catch title/value wiring bugs, unlike returning raw titles."""
    it = iter(answers)

    def fake_select(message, choices):
        answer = next(it)
        if answer is None:
            return FakeQuestion(None)
        for choice in choices:
            title, value = _title_and_value(choice)
            if title == answer:
                return FakeQuestion(value)
        raise AssertionError(f"no choice titled {answer!r} among {[c for c, _ in map(_title_and_value, choices)]}")

    monkeypatch.setattr(interactive_module.questionary, "select", fake_select)


def _checkbox_once(monkeypatch, answer_titles):
    def fake_checkbox(message, choices):
        selected = []
        for choice in choices:
            title, value = _title_and_value(choice)
            if title in answer_titles:
                selected.append(value)
        return FakeQuestion(selected)

    monkeypatch.setattr(interactive_module.questionary, "checkbox", fake_checkbox)


def _text_sequence(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(
        interactive_module.questionary,
        "text",
        lambda message, **kwargs: FakeQuestion(next(it)),
    )


def _confirm_sequence(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(
        interactive_module.questionary,
        "confirm",
        lambda *args, **kwargs: FakeQuestion(next(it)),
    )


def _series_client(episodes_by_season, series_id=6789, name="Example Series"):
    client = MagicMock()
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_streams.return_value = [
        SeriesStream(series_id=series_id, name=name, category_id="3")
    ]
    client.get_series_info.return_value = SeriesInfo(
        name=name, plot="", genre="", tmdb_id=None, seasons=[], episodes=episodes_by_season
    )
    return client


# --- category browsing ---------------------------------------------------


def test_browse_movies_by_category(monkeypatch):
    client = MagicMock()
    client.get_vod_categories.return_value = [Category(category_id="1", category_name="Action")]
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="Example Movie", category_id="1", container_extension="mkv")
    ]

    _select_sequence(monkeypatch, ["Movies", "Browse by category", "Action", "Done"])
    _checkbox_once(monkeypatch, ["Example Movie"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="movie", id=101)]
    client.get_vod_streams.assert_called_once_with(category_id="1")


def test_browse_movies_rename_prompt_overrides_display_name(monkeypatch):
    client = MagicMock()
    client.get_vod_categories.return_value = [Category(category_id="1", category_name="Action")]
    client.get_vod_streams.return_value = [
        VodStream(
            stream_id=101, name="NL ▎ Example Movie", category_id="1", container_extension="mkv"
        )
    ]

    _select_sequence(monkeypatch, ["Movies", "Browse by category", "Action", "Done"])
    _checkbox_once(monkeypatch, ["NL ▎ Example Movie"])
    _confirm_sequence(monkeypatch, [False])
    _text_sequence(monkeypatch, ["Example Movie"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="movie", id=101, display_name="Example Movie")]


def test_browse_movies_no_categories_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_vod_categories.return_value = []
    _select_sequence(monkeypatch, ["Movies", "Browse by category", "Done"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []


def test_browse_series_by_category_whole_series(monkeypatch):
    client = _series_client({1: [Episode(9001, 1, 1, "Pilot", "mkv")]})
    _select_sequence(
        monkeypatch,
        ["Series", "Browse by category", "Sci-Fi", "Example Series", "Whole series", "Done"],
    )

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789)]


def test_browse_series_rename_prompt_applies_to_all_returned_specs(monkeypatch):
    client = _series_client(
        {1: [Episode(9001, 1, 1, "Pilot", "mkv")], 2: [Episode(9002, 2, 1, "S2E1", "mkv")]},
        name="NL ▎ Example Series",
    )
    _select_sequence(
        monkeypatch,
        ["Series", "Browse by category", "Sci-Fi", "NL ▎ Example Series", "One or more seasons", "Done"],
    )
    _checkbox_once(monkeypatch, ["1", "2"])
    _confirm_sequence(monkeypatch, [False])
    _text_sequence(monkeypatch, ["Example Series"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [
        JobSpec(kind="series", id=6789, season=1, display_name="Example Series"),
        JobSpec(kind="series", id=6789, season=2, display_name="Example Series"),
    ]


def test_browse_series_keeping_name_leaves_display_name_none(monkeypatch):
    client = _series_client({1: [Episode(9001, 1, 1, "Pilot", "mkv")]})
    _select_sequence(
        monkeypatch,
        ["Series", "Browse by category", "Sci-Fi", "Example Series", "Whole series", "Done"],
    )
    _confirm_sequence(monkeypatch, [True])  # keep as-is: no "New name:" prompt should fire

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789)]


def test_browse_series_by_category_specific_season(monkeypatch):
    client = _series_client(
        {1: [Episode(9001, 1, 1, "Pilot", "mkv")], 2: [Episode(9002, 2, 1, "S2E1", "mkv")]}
    )
    _select_sequence(
        monkeypatch,
        [
            "Series",
            "Browse by category",
            "Sci-Fi",
            "Example Series",
            "One or more seasons",
            "Done",
        ],
    )
    _checkbox_once(monkeypatch, ["2"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789, season=2)]


def test_browse_series_by_category_multiple_seasons(monkeypatch):
    client = _series_client(
        {
            1: [Episode(9001, 1, 1, "Pilot", "mkv")],
            2: [Episode(9002, 2, 1, "S2E1", "mkv")],
            3: [Episode(9003, 3, 1, "S3E1", "mkv")],
        }
    )
    _select_sequence(
        monkeypatch,
        [
            "Series",
            "Browse by category",
            "Sci-Fi",
            "Example Series",
            "One or more seasons",
            "Done",
        ],
    )
    _checkbox_once(monkeypatch, ["1", "3"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [
        JobSpec(kind="series", id=6789, season=1),
        JobSpec(kind="series", id=6789, season=3),
    ]


def test_browse_series_by_category_specific_episode(monkeypatch):
    client = _series_client(
        {1: [Episode(9001, 1, 1, "Pilot", "mkv"), Episode(9002, 1, 2, "Ep2", "mkv")]}
    )
    _select_sequence(
        monkeypatch,
        [
            "Series",
            "Browse by category",
            "Sci-Fi",
            "Example Series",
            "A specific episode",
            "1",
            "2",
            "Done",
        ],
    )

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789, season=1, episode=2)]


def test_browse_cancelled_at_top_menu_returns_empty(monkeypatch):
    _select_sequence(monkeypatch, [None])
    specs = interactive_module.browse_and_select(MagicMock())
    assert specs == []


def test_browse_series_by_category_cancelled_at_category_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    _select_sequence(monkeypatch, ["Series", "Browse by category", None, "Done"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []


# --- search ---------------------------------------------------


def test_search_movies_across_categories(monkeypatch):
    client = MagicMock()
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="Wolfs (2024)", category_id="1", container_extension="mp4"),
        VodStream(stream_id=102, name="Wolf Creek", category_id="2", container_extension="mp4"),
        VodStream(stream_id=103, name="Unrelated Movie", category_id="1", container_extension="mp4"),
    ]
    client.get_vod_categories.return_value = [
        Category(category_id="1", category_name="Action"),
        Category(category_id="2", category_name="Horror"),
    ]

    _select_sequence(monkeypatch, ["Movies", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["wolf"])
    _checkbox_once(monkeypatch, ["Wolfs (2024)  [Action]", "Wolf Creek  [Horror]"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="movie", id=101), JobSpec(kind="movie", id=102)]
    # category browsing wasn't touched — search doesn't require picking one first
    client.get_vod_streams.assert_called_once_with()


def test_search_movies_no_matches_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="Something Else", category_id="1", container_extension="mp4")
    ]
    client.get_vod_categories.return_value = []

    _select_sequence(monkeypatch, ["Movies", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["wolf"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []


def test_search_movies_empty_query_skips_fetch_entirely(monkeypatch):
    client = MagicMock()

    _select_sequence(monkeypatch, ["Movies", "Search by name", "Done"])
    _text_sequence(monkeypatch, [""])

    specs = interactive_module.browse_and_select(client)

    assert specs == []
    client.get_vod_streams.assert_not_called()
    client.get_vod_categories.assert_not_called()


def test_search_movies_is_case_insensitive(monkeypatch):
    client = MagicMock()
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="WOLFS", category_id="1", container_extension="mp4")
    ]
    client.get_vod_categories.return_value = [Category(category_id="1", category_name="Action")]

    _select_sequence(monkeypatch, ["Movies", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["wolf"])
    _checkbox_once(monkeypatch, ["WOLFS  [Action]"])

    specs = interactive_module.browse_and_select(client)
    assert specs == [JobSpec(kind="movie", id=101)]


def test_search_movies_caches_catalog_across_repeated_searches(monkeypatch):
    client = MagicMock()
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="Wolfs", category_id="1", container_extension="mp4")
    ]
    client.get_vod_categories.return_value = [Category(category_id="1", category_name="Action")]

    _select_sequence(
        monkeypatch,
        ["Movies", "Search by name", "Movies", "Search by name", "Done"],
    )
    _text_sequence(monkeypatch, ["wolf", "wolf"])
    _checkbox_once(monkeypatch, ["Wolfs  [Action]"])

    interactive_module.browse_and_select(client)

    client.get_vod_streams.assert_called_once()
    client.get_vod_categories.assert_called_once()


def test_search_series_selects_whole_series_scope(monkeypatch):
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Example Series", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.return_value = SeriesInfo(
        name="Example Series",
        plot="",
        genre="",
        tmdb_id=None,
        seasons=[],
        episodes={1: [Episode(9001, 1, 1, "Pilot", "mkv")]},
    )

    _select_sequence(monkeypatch, ["Series", "Search by name", "Whole series", "Done"])
    _text_sequence(monkeypatch, ["example"])
    _checkbox_once(monkeypatch, ["Example Series  [Sci-Fi]  (1 season(s), 1 episode(s))"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789)]


def test_search_series_prompts_scope_per_selected_series(monkeypatch):
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=1, name="Alpha Show", category_id="3"),
        SeriesStream(series_id=2, name="Alpha Two", category_id="3"),
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]

    def fake_series_info(series_id):
        return SeriesInfo(
            name="Alpha Show" if series_id == 1 else "Alpha Two",
            plot="",
            genre="",
            tmdb_id=None,
            seasons=[],
            episodes={1: [Episode(9001 + series_id, 1, 1, "Pilot", "mkv")]},
        )

    client.get_series_info.side_effect = fake_series_info

    _select_sequence(
        monkeypatch,
        ["Series", "Search by name", "Whole series", "Whole series", "Done"],
    )
    _text_sequence(monkeypatch, ["alpha"])
    _checkbox_once(
        monkeypatch,
        [
            "Alpha Show  [Sci-Fi]  (1 season(s), 1 episode(s))",
            "Alpha Two  [Sci-Fi]  (1 season(s), 1 episode(s))",
        ],
    )

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=1), JobSpec(kind="series", id=2)]


def test_search_series_reuses_prefetched_info_no_double_fetch(monkeypatch):
    """The season/episode enrichment fetch during search must be reused when
    the user then picks a scope for that series — not fetched again."""
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Example Series", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.return_value = SeriesInfo(
        name="Example Series",
        plot="",
        genre="",
        tmdb_id=None,
        seasons=[],
        episodes={1: [Episode(9001, 1, 1, "Pilot", "mkv")]},
    )

    _select_sequence(monkeypatch, ["Series", "Search by name", "Whole series", "Done"])
    _text_sequence(monkeypatch, ["example"])
    _checkbox_once(monkeypatch, ["Example Series  [Sci-Fi]  (1 season(s), 1 episode(s))"])

    interactive_module.browse_and_select(client)

    client.get_series_info.assert_called_once_with(6789)


def test_search_series_label_shows_gap_marker(monkeypatch):
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Gappy Show", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.return_value = SeriesInfo(
        name="Gappy Show",
        plot="",
        genre="",
        tmdb_id=None,
        seasons=[],
        episodes={1: [Episode(9001, 1, 1, "E1", "mkv"), Episode(9003, 1, 3, "E3", "mkv")]},
    )

    _select_sequence(monkeypatch, ["Series", "Search by name", "Whole series", "Done"])
    _text_sequence(monkeypatch, ["gappy"])
    _checkbox_once(monkeypatch, ["Gappy Show  [Sci-Fi]  (1 season(s), 2 episode(s))  [gaps]"])

    specs = interactive_module.browse_and_select(client)
    assert specs == [JobSpec(kind="series", id=6789)]


def test_search_series_prints_gap_detail_upfront(monkeypatch, capsys):
    """Same-name duplicate series listings (different upstream providers) are
    exactly the case where you need to know *which* episode a copy is
    missing before picking one — the compact "[gaps]" marker alone doesn't
    say that. The full report must be visible before the checkbox prompt,
    not only after committing to a scope for one specific copy."""
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Gappy Show", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.return_value = SeriesInfo(
        name="Gappy Show",
        plot="",
        genre="",
        tmdb_id=None,
        seasons=[],
        episodes={1: [Episode(9001, 1, 1, "E1", "mkv"), Episode(9003, 1, 3, "E3", "mkv")]},
    )

    _select_sequence(monkeypatch, ["Series", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["gappy"])
    _checkbox_once(monkeypatch, [])  # nothing selected — just inspect what got printed

    interactive_module.browse_and_select(client)

    assert "S01E02" in capsys.readouterr().out


def test_search_series_prints_duplicate_detail_upfront(monkeypatch, capsys):
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Dupey Show", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.return_value = SeriesInfo(
        name="Dupey Show",
        plot="",
        genre="",
        tmdb_id=None,
        seasons=[],
        episodes={1: [Episode(9001, 1, 1, "E1", "mkv"), Episode(9002, 1, 1, "E1 dupe", "mkv")]},
    )

    _select_sequence(monkeypatch, ["Series", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["dupey"])
    _checkbox_once(monkeypatch, [])

    interactive_module.browse_and_select(client)

    out = capsys.readouterr().out
    assert "S01E01" in out
    assert "duplicate" in out.lower()


def test_search_series_cross_checks_duplicate_listings_for_trailing_gaps(monkeypatch, capsys):
    """The real bug this closes: a listing whose season ends at E51 looks
    complete on its own (detect_gaps_in_series can't tell that from "hasn't
    aired yet"). Only comparing it against a sibling listing that has E52
    reveals the gap — confirmed against a real "Bluey" catalog with exactly
    this shape (EN/DUAL copies had S01 1-52, the NL copy only had 1-51)."""
    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=1, name="Show A", category_id="3"),
        SeriesStream(series_id=2, name="Show B", category_id="3"),
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Kids")]

    def fake_series_info(series_id):
        if series_id == 1:
            episodes = {1: [Episode(9000 + n, 1, n, f"E{n}", "mkv") for n in range(1, 53)]}
        else:
            episodes = {1: [Episode(9100 + n, 1, n, f"E{n}", "mkv") for n in range(1, 52)]}
        return SeriesInfo(
            name="Show A" if series_id == 1 else "Show B",
            plot="",
            genre="",
            tmdb_id=None,
            seasons=[],
            episodes=episodes,
        )

    client.get_series_info.side_effect = fake_series_info

    _select_sequence(monkeypatch, ["Series", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["show"])
    _checkbox_once(monkeypatch, [])  # just inspect what got printed, don't select anything

    interactive_module.browse_and_select(client)

    out = capsys.readouterr().out
    # Show A is complete (1-52) so it prints nothing; only Show B (1-51,
    # missing E52 relative to its sibling) gets a report.
    assert "Show A" not in out
    assert "Show B" in out
    assert "S01E52" in out


def test_search_series_handles_info_fetch_failure_gracefully(monkeypatch):
    """A series whose enrichment fetch failed can still be selected — picking
    it must not crash the session, just report and skip it (matching the
    original get_series_info call failing again on retry, since info=None)."""
    from xc_vod_dl.exceptions import XtreamAPIError

    client = MagicMock()
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Broken Show", category_id="3")
    ]
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_info.side_effect = XtreamAPIError("boom")

    _select_sequence(monkeypatch, ["Series", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["broken"])
    _checkbox_once(monkeypatch, ["Broken Show  [Sci-Fi]  (season info unavailable)"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []
