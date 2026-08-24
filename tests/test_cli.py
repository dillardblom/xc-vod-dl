import shutil
from pathlib import Path

import pytest
import questionary
from click.testing import CliRunner

from xc_vod_dl.cli import main
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.ui import interactive as interactive_module

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available on PATH"),
]


class _FakeQuestion:
    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


def _title_and_value(choice):
    # Plain strings have a `.title` *method*, so hasattr(choice, "title") is
    # true for them too — must check the actual Choice type instead.
    if isinstance(choice, questionary.Choice):
        return choice.title, choice.value
    return choice, choice


def _select_sequence(monkeypatch, answers):
    it = iter(answers)

    def fake_select(message, choices):
        answer = next(it)
        if answer is None:
            return _FakeQuestion(None)
        for choice in choices:
            title, value = _title_and_value(choice)
            if title == answer:
                return _FakeQuestion(value)
        raise AssertionError(f"no choice titled {answer!r}")

    monkeypatch.setattr(interactive_module.questionary, "select", fake_select)


def _checkbox_once(monkeypatch, answer_titles):
    def fake_checkbox(message, choices):
        selected = [v for t, v in (_title_and_value(c) for c in choices) if t in answer_titles]
        return _FakeQuestion(selected)

    monkeypatch.setattr(interactive_module.questionary, "checkbox", fake_checkbox)


def _text_sequence(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(
        interactive_module.questionary, "text", lambda message: _FakeQuestion(next(it))
    )


def _set_env(monkeypatch, base_url):
    monkeypatch.setenv("XCVODDL_SERVER", base_url)
    monkeypatch.setenv("XCVODDL_USERNAME", "demo")
    monkeypatch.setenv("XCVODDL_PASSWORD", "demo")


def test_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "xc-vod-dl" in result.output


def test_fetch_movie_end_to_end(xtream_server, monkeypatch, tmp_path):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_info"]["101"] = {
        "info": {
            "tmdb_id": 550,
            "name": "Example Movie",
            "plot": "A movie used as a test fixture.",
            "genre": "Drama",
            "releasedate": "2024-03-15",
        },
        "movie_data": {
            "stream_id": 101,
            "name": "Example Movie (2024)",
            "container_extension": "mp4",
            "category_id": "1",
        },
    }

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("movie:101\n")
        result = runner.invoke(main, ["fetch", "-f", "manifest.txt", "-y", "--serial"])

        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        movie_path = Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).mp4"
        nfo_path = Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).nfo"
        assert movie_path.exists()
        assert nfo_path.exists()
        assert "<title>Example Movie (2024)</title>" in nfo_path.read_text()


def test_fetch_series_season_end_to_end(xtream_server, monkeypatch, tmp_path):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 2}],
        "info": {"name": "Example Series", "plot": "A series.", "genre": "Sci-Fi", "tmdb_id": 12345},
        "episodes": {
            "1": [
                {
                    "id": "9001",
                    "episode_num": 1,
                    "title": "Pilot",
                    "container_extension": "mp4",
                    "season": 1,
                    "info": {},
                },
                {
                    "id": "9002",
                    "episode_num": 2,
                    "title": "Episode Two",
                    "container_extension": "mp4",
                    "season": 1,
                    "info": {},
                },
            ]
        },
    }

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("series:6789:1\n")
        result = runner.invoke(main, ["fetch", "-f", "manifest.txt", "-y", "--serial"])

        assert result.exit_code == 0, result.output
        assert "2 succeeded" in result.output
        season_dir = Path("Series") / "Example Series" / "Season 01"
        assert (season_dir / "Example Series - S01E01 - Pilot.mp4").exists()
        assert (season_dir / "Example Series - S01E02 - Episode Two.mp4").exists()
        assert (Path("Series") / "Example Series" / "tvshow.nfo").exists()


def test_fetch_declined_confirmation_does_not_download(xtream_server, monkeypatch, tmp_path):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_info"]["101"] = {
        "info": {"name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "Example Movie (2024)", "container_extension": "mp4"},
    }

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("movie:101\n")
        runner.invoke(main, ["fetch", "-f", "manifest.txt"], input="n\n")

        assert not Path("Movies").exists()


def test_fetch_missing_manifest_file_errors():
    result = CliRunner().invoke(main, ["fetch", "-f", "does-not-exist.txt", "-y"])
    assert result.exit_code != 0


def test_fetch_missing_credentials_exits_with_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("XCVODDL_SERVER", raising=False)
    monkeypatch.delenv("XCVODDL_USERNAME", raising=False)
    monkeypatch.delenv("XCVODDL_PASSWORD", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("movie:101\n")
        result = runner.invoke(
            main, ["fetch", "-f", "manifest.txt", "-y", "--config", str(tmp_path / "nope.toml")]
        )
        assert result.exit_code == 2


def test_gaps_command_reports_missing_episode(xtream_server, monkeypatch):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 3}],
        "info": {"name": "Example Series"},
        "episodes": {
            "1": [
                {"id": "1", "episode_num": 1, "title": "E1", "container_extension": "mp4", "season": 1, "info": {}},
                {"id": "3", "episode_num": 3, "title": "E3", "container_extension": "mp4", "season": 1, "info": {}},
            ]
        },
    }
    result = CliRunner().invoke(main, ["gaps", "--series-id", "6789"])
    assert result.exit_code == 1
    assert "S01E02" in result.output


def test_gaps_command_json_output(xtream_server, monkeypatch):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [],
        "info": {"name": "Example Series"},
        "episodes": {
            "1": [
                {"id": "1", "episode_num": 1, "title": "E1", "container_extension": "mp4", "season": 1, "info": {}},
                {"id": "2", "episode_num": 2, "title": "E2", "container_extension": "mp4", "season": 1, "info": {}},
            ]
        },
    }
    result = CliRunner().invoke(main, ["gaps", "--series-id", "6789", "--json"])
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_browse_command_end_to_end(xtream_server, monkeypatch, tmp_path):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_categories"] = [{"category_id": "1", "category_name": "Action"}]
    state["vod_streams"] = [
        {"stream_id": 101, "name": "Example Movie", "category_id": "1", "container_extension": "mp4"}
    ]
    state["vod_info"]["101"] = {
        "info": {"name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "Example Movie", "container_extension": "mp4"},
    }

    _select_sequence(monkeypatch, ["Movies", "Browse by category", "Action", "Done"])
    _checkbox_once(monkeypatch, ["Example Movie"])

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["browse", "--serial"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        assert (Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).mp4").exists()


def test_browse_command_series_end_to_end(xtream_server, monkeypatch, tmp_path):
    """End-to-end regression test for the get_series/get_series_streams action-name
    mismatch found against a real Dispatcharr server: this drives the full
    browse -> client.get_series_streams() -> real HTTP call -> download path,
    so a wrong action name fails loudly instead of silently skipping series."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_categories"] = [{"category_id": "3", "category_name": "Sci-Fi"}]
    state["series_streams"] = [
        {"series_id": 6789, "name": "Example Series", "category_id": "3"}
    ]
    state["series_info"]["6789"] = {
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
        "info": {"name": "Example Series"},
        "episodes": {
            "1": [
                {
                    "id": "9001",
                    "episode_num": 1,
                    "title": "Pilot",
                    "container_extension": "mp4",
                    "season": 1,
                    "info": {},
                }
            ]
        },
    }

    _select_sequence(
        monkeypatch,
        ["Series", "Browse by category", "Sci-Fi", "Example Series", "Whole series", "Done"],
    )

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["browse", "--serial"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        episode_path = (
            Path("Series") / "Example Series" / "Season 01" / "Example Series - S01E01 - Pilot.mp4"
        )
        assert episode_path.exists()


def test_browse_command_search_end_to_end(xtream_server, monkeypatch, tmp_path):
    """Search spans categories: this movie is findable by name without ever
    picking 'Action' as a category first."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_categories"] = [{"category_id": "1", "category_name": "Action"}]
    state["vod_streams"] = [
        {"stream_id": 101, "name": "Example Movie", "category_id": "1", "container_extension": "mp4"},
        {"stream_id": 102, "name": "Unrelated", "category_id": "1", "container_extension": "mp4"},
    ]
    state["vod_info"]["101"] = {
        "info": {"name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "Example Movie", "container_extension": "mp4"},
    }

    _select_sequence(monkeypatch, ["Movies", "Search by name", "Done"])
    _text_sequence(monkeypatch, ["example"])
    _checkbox_once(monkeypatch, ["Example Movie  [Action]"])

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["browse", "--serial"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        assert (Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).mp4").exists()


def test_browse_command_nothing_selected_downloads_nothing(xtream_server, monkeypatch, tmp_path):
    base_url, _state = xtream_server
    _set_env(monkeypatch, base_url)
    _select_sequence(monkeypatch, [None])

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["browse"])
        assert result.exit_code == 0
        assert not Path("Movies").exists()


def test_resume_retries_incomplete_items_without_a_manifest(xtream_server, monkeypatch, tmp_path):
    base_url, _state = xtream_server
    _set_env(monkeypatch, base_url)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        target = Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).mp4"
        with StateStore(Path("state.db")) as store:
            store.upsert_pending(
                id="movie:101",
                kind="movie",
                title="Example Movie (2024)",
                target_path=str(target),
                container_extension="mp4",
            )
            store.mark_status("movie:101", "failed", last_error="simulated prior failure")

        result = runner.invoke(main, ["resume", "--serial"])

        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        assert target.exists()
        with StateStore(Path("state.db")) as store:
            assert store.get("movie:101").status == "done"


def test_resume_with_no_state_db_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("XCVODDL_SERVER", "http://127.0.0.1:1")
    monkeypatch.setenv("XCVODDL_USERNAME", "demo")
    monkeypatch.setenv("XCVODDL_PASSWORD", "demo")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "nothing to resume" in result.output


def test_resume_with_nothing_incomplete_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("XCVODDL_SERVER", "http://127.0.0.1:1")
    monkeypatch.setenv("XCVODDL_USERNAME", "demo")
    monkeypatch.setenv("XCVODDL_PASSWORD", "demo")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        with StateStore(Path("state.db")) as store:
            store.upsert_pending(id="movie:1", kind="movie", title="x", target_path="x.mp4")
            store.mark_status("movie:1", "done")

        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0
        assert "nothing to resume" in result.output


def test_clean_removes_stray_voddl_files(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        stray = Path("Movies") / "Example (2024)" / "Example (2024).mp4.voddl"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"partial")
        keep = Path("Movies") / "Example (2024)" / "Example (2024).mp4"
        keep.write_bytes(b"done")

        result = runner.invoke(main, ["clean", "-y"])

        assert result.exit_code == 0
        assert not stray.exists()
        assert keep.exists()


def test_clean_with_nothing_to_clean(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["clean"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output


def test_clean_asks_for_confirmation_by_default(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        stray = Path("movie.mkv.voddl")
        stray.write_bytes(b"partial")

        runner.invoke(main, ["clean"], input="n\n")

        assert stray.exists()  # declined deletion
