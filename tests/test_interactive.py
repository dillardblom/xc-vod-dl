from unittest.mock import MagicMock

from xc_vod_dl.api.models import Category, Episode, SeriesInfo, SeriesStream, VodStream
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.ui import interactive as interactive_module


class FakeQuestion:
    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


def _select_sequence(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(
        interactive_module.questionary, "select", lambda message, choices: FakeQuestion(next(it))
    )


def _checkbox_once(monkeypatch, answer):
    monkeypatch.setattr(
        interactive_module.questionary, "checkbox", lambda message, choices: FakeQuestion(answer)
    )


def _series_client(episodes_by_season):
    client = MagicMock()
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    client.get_series_streams.return_value = [
        SeriesStream(series_id=6789, name="Example Series", category_id="3")
    ]
    client.get_series_info.return_value = SeriesInfo(
        name="Example Series", plot="", genre="", tmdb_id=None, seasons=[], episodes=episodes_by_season
    )
    return client


def test_browse_movies_selects_category_and_movies(monkeypatch):
    client = MagicMock()
    client.get_vod_categories.return_value = [Category(category_id="1", category_name="Action")]
    client.get_vod_streams.return_value = [
        VodStream(stream_id=101, name="Example Movie", category_id="1", container_extension="mkv")
    ]

    _select_sequence(monkeypatch, ["Movies", "Action", "Done"])
    _checkbox_once(monkeypatch, ["Example Movie"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="movie", id=101)]
    client.get_vod_streams.assert_called_once_with(category_id="1")


def test_browse_movies_no_categories_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_vod_categories.return_value = []
    _select_sequence(monkeypatch, ["Movies", "Done"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []


def test_browse_series_whole_series(monkeypatch):
    client = _series_client({1: [Episode(9001, 1, 1, "Pilot", "mkv")]})
    _select_sequence(monkeypatch, ["Series", "Sci-Fi", "Example Series", "Whole series", "Done"])

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789)]


def test_browse_series_specific_season(monkeypatch):
    client = _series_client(
        {
            1: [Episode(9001, 1, 1, "Pilot", "mkv")],
            2: [Episode(9002, 2, 1, "S2E1", "mkv")],
        }
    )
    _select_sequence(
        monkeypatch,
        ["Series", "Sci-Fi", "Example Series", "A specific season", "2", "Done"],
    )

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789, season=2)]


def test_browse_series_specific_episode(monkeypatch):
    client = _series_client(
        {1: [Episode(9001, 1, 1, "Pilot", "mkv"), Episode(9002, 1, 2, "Ep2", "mkv")]}
    )
    _select_sequence(
        monkeypatch,
        ["Series", "Sci-Fi", "Example Series", "A specific episode", "1", "2", "Done"],
    )

    specs = interactive_module.browse_and_select(client)

    assert specs == [JobSpec(kind="series", id=6789, season=1, episode=2)]


def test_browse_cancelled_at_top_menu_returns_empty(monkeypatch):
    _select_sequence(monkeypatch, [None])
    specs = interactive_module.browse_and_select(MagicMock())
    assert specs == []


def test_browse_series_cancelled_at_category_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_series_categories.return_value = [Category(category_id="3", category_name="Sci-Fi")]
    _select_sequence(monkeypatch, ["Series", None, "Done"])

    specs = interactive_module.browse_and_select(client)
    assert specs == []
