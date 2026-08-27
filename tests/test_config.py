from pathlib import Path

import pytest

from xc_vod_dl.config import (
    _resolve_default_config_path,
    default_config_path,
    load_config,
)
from xc_vod_dl.exceptions import ConfigError


def test_resolve_default_config_path_prefers_local_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[account]\n")
    assert _resolve_default_config_path() == Path("config.toml")


def test_resolve_default_config_path_falls_back_to_xdg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # no local config.toml present
    assert _resolve_default_config_path() == default_config_path()


def test_load_config_reads_account_from_file(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[account]\nserver = "https://example.com"\nusername = "u"\npassword = "p"\n'
    )
    config = load_config(config_file)
    assert config.account.server == "https://example.com"
    assert config.account.username == "u"
    assert config.account.password == "p"


def test_load_config_cli_flags_override_file(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[account]\nserver = "https://from-file.example.com"\nusername = "file-user"\npassword = "file-pass"\n'
    )
    config = load_config(config_file, server="https://cli.example.com", username="cli-user")
    assert config.account.server == "https://cli.example.com"
    assert config.account.username == "cli-user"
    assert config.account.password == "file-pass"  # not overridden, falls through to file


def test_load_config_env_vars_override_file_but_not_cli(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[account]\nserver = "https://from-file.example.com"\nusername = "file-user"\npassword = "file-pass"\n'
    )
    monkeypatch.setenv("XCVODDL_SERVER", "https://from-env.example.com")
    monkeypatch.setenv("XCVODDL_USERNAME", "env-user")

    config = load_config(config_file, username="cli-user")

    assert config.account.server == "https://from-env.example.com"  # env beats file
    assert config.account.username == "cli-user"  # CLI beats env
    assert config.account.password == "file-pass"  # falls through to file


def test_load_config_missing_credentials_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("XCVODDL_SERVER", raising=False)
    monkeypatch.delenv("XCVODDL_USERNAME", raising=False)
    monkeypatch.delenv("XCVODDL_PASSWORD", raising=False)
    empty_config = tmp_path / "config.toml"
    empty_config.write_text("")

    with pytest.raises(ConfigError, match="server"):
        load_config(empty_config)


def test_load_config_parses_download_and_concurrency_sections(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[account]
server = "https://example.com"
username = "u"
password = "p"

[download]
movies_dir = "Films"
verify_mode = "full"
download_cover = false
state_db = "/local/state/xc-vod-dl/state.db"

[concurrency]
max_parallel_ceiling = 2
safety_margin = 0
"""
    )
    config = load_config(config_file)
    assert config.download.movies_dir == Path("Films")
    assert config.download.verify_mode == "full"
    assert config.download.download_cover is False
    assert config.download.download_nfo is True  # unset, keeps default
    assert config.download.state_db == Path("/local/state/xc-vod-dl/state.db")
    assert config.concurrency.max_parallel_ceiling == 2
    assert config.concurrency.safety_margin == 0
    assert config.concurrency.cooldown_s == 30.0  # unset, keeps default


def test_load_config_missing_file_uses_defaults_when_credentials_supplied_otherwise(tmp_path):
    missing_path = tmp_path / "does-not-exist.toml"
    config = load_config(missing_path, server="https://example.com", username="u", password="p")
    assert config.account.server == "https://example.com"
    assert config.download.movies_dir == Path("Movies")
    assert config.download.state_db == Path("state.db")
