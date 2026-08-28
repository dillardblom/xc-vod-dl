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
        interactive_module.questionary,
        "text",
        lambda message, **kwargs: _FakeQuestion(next(it)),
    )


@pytest.fixture(autouse=True)
def _default_keep_name_as_is(monkeypatch):
    """questionary.confirm now backs the optional post-selection rename
    prompt — default every test to "keep the name as-is" so existing
    end-to-end browse tests don't need to know about it."""
    monkeypatch.setattr(
        interactive_module.questionary, "confirm", lambda *args, **kwargs: _FakeQuestion(True)
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


def test_fetch_state_db_override_writes_outside_the_download_directory(
    xtream_server, monkeypatch, tmp_path
):
    """--state-db lets the SQLite state file live on local disk even when the
    download directory itself is on a network share (CIFS/NFS don't reliably
    support the file locking SQLite needs, causing spurious "database is
    locked" errors)."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_info"]["101"] = {
        "info": {"tmdb_id": 550, "name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {
            "stream_id": 101,
            "name": "Example Movie (2024)",
            "container_extension": "mp4",
            "category_id": "1",
        },
    }
    local_state_dir = tmp_path / "local-state"
    local_state_dir.mkdir()
    state_db_path = local_state_dir / "state.db"

    runner = CliRunner()
    download_dir = tmp_path / "download-dir"
    download_dir.mkdir()
    with runner.isolated_filesystem(temp_dir=download_dir):
        Path("manifest.txt").write_text("movie:101\n")
        result = runner.invoke(
            main,
            ["fetch", "-f", "manifest.txt", "-y", "--serial", "--state-db", str(state_db_path)],
        )

        assert result.exit_code == 0, result.output
        assert not Path("state.db").exists()

    assert state_db_path.exists()
    with StateStore(state_db_path) as store:
        assert store.get("movie:101").status == "done"


def test_resolve_movie_display_name_overrides_folder_and_nfo_title(xtream_server, monkeypatch, tmp_path):
    """The interactive rename prompt only produces a JobSpec.display_name —
    this checks the resolver actually honors it end to end, not just that
    the interactive layer records the choice."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_info"]["101"] = {
        "info": {"name": "NL Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {
            "stream_id": 101,
            "name": "NL Example Movie",
            "container_extension": "mp4",
            "category_id": "1",
        },
    }

    from xc_vod_dl.cli import _resolve_movie
    from xc_vod_dl.config import Config, load_config

    config: Config = load_config(server=base_url, username="demo", password="demo")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        import requests

        from xc_vod_dl.api.client import XtreamClient

        client = XtreamClient(base_url, "demo", "demo")
        job, write_metadata = _resolve_movie(
            client, config, requests.Session(), 101, "Example Movie"
        )
        write_metadata()

        assert job.title == "Example Movie"
        # _movie_target appends the release year since the renamed name
        # doesn't already end in one.
        target_dir = Path("Movies") / "Example Movie (2024)"
        assert job.target_path == target_dir / "Example Movie (2024).mp4"
        nfo = (target_dir / "Example Movie (2024).nfo").read_text()
        assert "<title>Example Movie</title>" in nfo


def test_resolve_series_display_name_overrides_dir_titles_and_episode_showtitle(
    xtream_server, monkeypatch, tmp_path
):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
        "info": {"name": "NL Example Series", "plot": "A series."},
        "episodes": {
            "1": [
                {
                    "id": "9001",
                    "episode_num": 1,
                    "title": "Pilot",
                    "container_extension": "mkv",
                    "season": 1,
                    "info": {},
                }
            ]
        },
    }

    from xc_vod_dl.cli import _resolve_series
    from xc_vod_dl.config import Config, load_config

    config: Config = load_config(server=base_url, username="demo", password="demo")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        import requests

        from xc_vod_dl.api.client import XtreamClient

        client = XtreamClient(base_url, "demo", "demo")
        jobs, writers = _resolve_series(
            client, config, requests.Session(), 6789, None, None, "Example Series"
        )
        for writer in writers:
            writer()

        series_dir = Path("Series") / "Example Series"
        assert jobs[0].target_path == series_dir / "Season 01" / "Example Series - S01E01 - Pilot.mkv"

        tvshow_nfo = (series_dir / "tvshow.nfo").read_text()
        assert "<title>Example Series</title>" in tvshow_nfo

        episode_nfo = (series_dir / "Season 01" / "Example Series - S01E01 - Pilot.nfo").read_text()
        assert "<showtitle>Example Series</showtitle>" in episode_nfo


def test_fetch_writes_logfile_with_timestamps(xtream_server, monkeypatch, tmp_path):
    """config.toml's `logfile` used to be defined but never actually wired up
    to real file logging — everything only went to the console, so there was
    no way to review what a run did (e.g. concurrency throttle decisions)
    after the terminal was gone."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["vod_info"]["101"] = {
        "info": {"name": "Example Movie", "releasedate": "2024-03-15"},
        "movie_data": {"stream_id": 101, "name": "Example Movie", "container_extension": "mp4"},
    }

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("movie:101\n")
        result = runner.invoke(main, ["fetch", "-f", "manifest.txt", "-y", "--serial"])

        assert result.exit_code == 0, result.output
        log_path = Path("voddl.log")
        assert log_path.exists()
        content = log_path.read_text()
        assert content.strip()
        # timestamped, not just the bare console message
        assert content.splitlines()[0][:4].isdigit()  # starts with a year
        assert "starting download: movie:101" in content
        assert "finished download: movie:101 -> done" in content


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


def test_fetch_series_disambiguates_colliding_episode_targets(xtream_server, monkeypatch, tmp_path):
    """Real-world regression: an upstream source returned two distinct episode
    ids both claiming season 1 episode 8 with the same title text (sloppy
    metadata on their end). Both must still land on disk under distinct
    filenames instead of racing to write the same .voddl file."""
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
        "info": {"name": "Example Series"},
        "episodes": {
            "1": [
                {
                    "id": "9001",
                    "episode_num": 8,
                    "title": "Same Title",
                    "container_extension": "mp4",
                    "season": 1,
                    "info": {},
                },
                {
                    "id": "9002",
                    "episode_num": 8,
                    "title": "Same Title",
                    "container_extension": "mp4",
                    "season": 1,
                    "info": {},
                },
            ]
        },
    }

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("manifest.txt").write_text("series:6789\n")
        result = runner.invoke(main, ["fetch", "-f", "manifest.txt", "-y", "--serial"])

        assert result.exit_code == 0, result.output
        assert "2 succeeded" in result.output
        season_dir = Path("Series") / "Example Series" / "Season 01"
        files = sorted(p.name for p in season_dir.glob("*.mp4"))
        assert len(files) == 2
        assert "Example Series - S01E08 - Same Title.mp4" in files
        assert any("[9002]" in f for f in files)


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
    assert result.output.strip() == '{"gaps": {}, "duplicates": {}}'


def test_gaps_command_reports_duplicate_episode(xtream_server, monkeypatch):
    base_url, state = xtream_server
    _set_env(monkeypatch, base_url)
    state["series_info"]["6789"] = {
        "seasons": [],
        "info": {"name": "Example Series"},
        "episodes": {
            "1": [
                {"id": "1", "episode_num": 1, "title": "E1", "container_extension": "mp4", "season": 1, "info": {}},
                {"id": "2", "episode_num": 1, "title": "E1 dupe", "container_extension": "mp4", "season": 1, "info": {}},
                {"id": "3", "episode_num": 2, "title": "E2", "container_extension": "mp4", "season": 1, "info": {}},
            ]
        },
    }
    result = CliRunner().invoke(main, ["gaps", "--series-id", "6789"])
    assert result.exit_code == 1
    assert "S01E01" in result.output
    assert "duplicate" in result.output.lower()


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


def test_resume_requeues_a_done_record_whose_file_is_missing_here(xtream_server, monkeypatch, tmp_path):
    """state.db can be shared across multiple download directories (e.g. an
    absolute configured state_db). A "done" record from a *different*
    directory must not make resume silently report "nothing to resume" here
    when the file was never actually downloaded into this one."""
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
            store.mark_status("movie:101", "done")  # done elsewhere; not actually here
        assert not target.exists()

        result = runner.invoke(main, ["resume", "--serial"])

        assert result.exit_code == 0, result.output
        assert "1 item(s) were marked done elsewhere but are missing here" in result.output
        assert "1 succeeded" in result.output
        assert target.exists()
        with StateStore(Path("state.db")) as store:
            assert store.get("movie:101").status == "done"


def test_resume_leaves_a_genuinely_done_record_alone(xtream_server, monkeypatch, tmp_path):
    base_url, _state = xtream_server
    _set_env(monkeypatch, base_url)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        target = Path("Movies") / "Example Movie (2024)" / "Example Movie (2024).mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"already really here")
        with StateStore(Path("state.db")) as store:
            store.upsert_pending(
                id="movie:101",
                kind="movie",
                title="Example Movie (2024)",
                target_path=str(target),
                container_extension="mp4",
            )
            store.mark_status("movie:101", "done")

        result = runner.invoke(main, ["resume"])

        assert result.exit_code == 0, result.output
        assert "were marked done" not in result.output
        assert "nothing to resume" in result.output
        assert target.read_bytes() == b"already really here"  # untouched


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
        Path("x.mp4").write_bytes(b"already really here")  # genuinely done, not just claimed
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


def test_run_pipeline_registers_all_jobs_before_running_any(tmp_path):
    """Regression test: killing a parallel run early used to leave state.db
    only knowing about whichever handful of jobs a worker thread had
    actually reached — anything still queued behind the concurrency ceiling
    was invisible to `resume`. Every job must be a row in state.db *before*
    _run_jobs (the actual download loop) is ever invoked."""
    from unittest.mock import MagicMock, patch

    import requests

    from xc_vod_dl.cli import _run_pipeline
    from xc_vod_dl.config import AccountConfig, Config
    from xc_vod_dl.download.engine import DownloadJob

    config = Config(account=AccountConfig(server="http://x", username="u", password="p"))
    jobs = [
        DownloadJob(
            id=f"movie:{i}",
            url="http://x/movie",
            target_path=tmp_path / f"m{i}.mp4",
            kind="movie",
            title=f"Movie {i}",
        )
        for i in range(5)
    ]

    seen_ids_at_run_time = []

    def fake_run_jobs(client, config, account, session, jobs, state, serial, parallel_override, verify_mode, reporter):
        seen_ids_at_run_time.extend(r.id for r in state.list_all())
        return {j.id: True for j in jobs}

    client = MagicMock()
    client.get_account.return_value = MagicMock(max_connections=4, active_cons=0)

    with StateStore(":memory:") as state, patch("xc_vod_dl.cli._run_jobs", side_effect=fake_run_jobs):
        _run_pipeline(
            client,
            config,
            requests.Session(),
            jobs,
            state,
            serial=True,
            parallel_override=None,
            verify_mode="quick",
            quiet=True,
        )

    assert set(seen_ids_at_run_time) == {j.id for j in jobs}


def test_run_pipeline_registration_is_idempotent_for_resume(tmp_path):
    """upsert_pending() must not clobber a status resume already knows about
    when _run_pipeline re-registers the same jobs."""
    from unittest.mock import MagicMock

    import requests

    from xc_vod_dl.cli import _run_pipeline
    from xc_vod_dl.config import AccountConfig, Config
    from xc_vod_dl.download.engine import DownloadJob

    config = Config(account=AccountConfig(server="http://x", username="u", password="p"))
    job = DownloadJob(
        id="movie:1", url="http://x/movie", target_path=tmp_path / "m.mp4", kind="movie", title="Movie"
    )
    client = MagicMock()
    client.get_account.return_value = MagicMock(max_connections=4, active_cons=0)

    with StateStore(":memory:") as state:
        state.upsert_pending(id="movie:1", kind="movie", title="Movie", target_path=str(job.target_path))
        state.mark_status("movie:1", "failed", last_error="previous attempt")

        from unittest.mock import patch

        with patch("xc_vod_dl.cli._run_jobs", return_value={"movie:1": True}):
            _run_pipeline(
                client,
                config,
                requests.Session(),
                [job],
                state,
                serial=True,
                parallel_override=None,
                verify_mode="quick",
                quiet=True,
            )

        record = state.get("movie:1")
        assert record.status == "failed"  # not silently reset to "pending"
        assert record.last_error == "previous attempt"
