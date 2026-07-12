"""Tests for quip2md.config."""

from pathlib import Path

import pytest

from quip2md.config import ConfigError, load_config


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("QUIP_TOKEN", raising=False)
    monkeypatch.delenv("QUIP_BASE_URL", raising=False)
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="QUIP_TOKEN"):
        load_config(env_path=empty_env)


def test_missing_dotenv_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("QUIP_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="QUIP_TOKEN"):
        load_config(env_path=tmp_path / "nonexistent.env")


def test_dotenv_value_used_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("QUIP_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("QUIP_TOKEN=from-dotenv\n", encoding="utf-8")

    config = load_config(env_path=env_file)

    assert config.token == "from-dotenv"


def test_env_var_overrides_dotenv_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("QUIP_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("QUIP_TOKEN", "from-process-env")

    config = load_config(env_path=env_file)

    assert config.token == "from-process-env"


def test_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QUIP_TOKEN", "a-token")
    monkeypatch.delenv("QUIP_BASE_URL", raising=False)
    env_file = tmp_path / ".env"

    config = load_config(env_path=env_file)

    assert config.base_url == "https://platform.quip.com"
    assert config.dry_run is False
    assert config.verbose is False
    assert config.include_chats is False
    assert config.force is False


def test_config_is_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QUIP_TOKEN", "a-token")
    env_file = tmp_path / ".env"

    config = load_config(env_path=env_file)

    field_name = "token"
    with pytest.raises(AttributeError):
        setattr(config, field_name, "mutated")
