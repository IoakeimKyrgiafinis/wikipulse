"""Smoke tests: the package imports and settings load with sensible defaults."""

from pathlib import Path

import pytest
from pydantic import ValidationError

import wikipulse
from wikipulse import check
from wikipulse.config import DEFAULT_USER_AGENT, Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove WikiPulse variables from the environment so defaults are visible."""
    for name in ("REDIS_URL", "POSTGRES_DSN", "USER_AGENT", "LOG_LEVEL", "DATA_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_package_imports() -> None:
    assert wikipulse.__version__


def test_settings_load_with_defaults(clean_env: None) -> None:
    settings = Settings(_env_file=None)  # ignore any local .env
    assert settings.redis_url.startswith("redis://")
    assert settings.postgres_dsn.startswith("postgresql://")
    assert settings.log_level == "INFO"
    assert settings.data_dir == Path("data")
    assert settings.user_agent == DEFAULT_USER_AGENT
    assert settings.user_agent_is_placeholder()


def test_settings_read_environment(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "USER_AGENT", "WikiPulse/0.1 (https://github.com/someone/wikipulse; a@b.org)"
    )
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert not settings.user_agent_is_placeholder()


def test_example_contact_counts_as_placeholder(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "USER_AGENT", "WikiPulse/0.1 (https://github.com/someone/wikipulse; you@example.com)"
    )
    assert Settings(_env_file=None).user_agent_is_placeholder()


def test_log_level_is_case_insensitive(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert Settings(_env_file=None).log_level == "WARNING"


def test_invalid_log_level_is_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)


def test_check_reports_invalid_settings_without_traceback(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    monkeypatch.setattr(check, "get_settings", lambda: Settings(_env_file=None))
    assert check.main() == 2
    err = capsys.readouterr().err
    assert "Invalid settings" in err
    assert "log_level" in err
