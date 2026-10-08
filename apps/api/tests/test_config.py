import pytest
from pydantic import ValidationError

from context_window.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "DATABASE_URL_DIRECT", "EMBEDDING_DIM", "BRIEFING_TZ"):
        monkeypatch.delenv(name, raising=False)


def _settings(**env: str) -> Settings:
    # _env_file=None isolates the test from the developer's real .env file.
    return Settings(_env_file=None, **env)  # pyright: ignore[reportCallIssue]


def test_defaults() -> None:
    settings = _settings()
    assert settings.embedding_dim == 1536
    assert settings.briefing_tz == "UTC"
    assert settings.database_url is None


def test_rejects_unknown_time_zone() -> None:
    with pytest.raises(ValidationError, match="unknown IANA time zone"):
        _settings(briefing_tz="Mars/Olympus_Mons")


def test_accepts_iana_time_zone() -> None:
    assert _settings(briefing_tz="Asia/Kolkata").briefing_tz == "Asia/Kolkata"


def test_direct_url_falls_back_to_pooled() -> None:
    settings = _settings(database_url="postgresql://u:p@h-pooler/db")
    assert settings.require_direct_url() == "postgresql://u:p@h-pooler/db"


def test_prefers_direct_url() -> None:
    settings = _settings(
        database_url="postgresql://u:p@h-pooler/db", database_url_direct="postgresql://u:p@h/db"
    )
    assert settings.require_direct_url() == "postgresql://u:p@h/db"
    assert settings.require_pooled_url() == "postgresql://u:p@h-pooler/db"


def test_missing_url_raises_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _settings().require_direct_url()


def test_urls_are_secret_in_repr() -> None:
    settings = _settings(database_url="postgresql://u:hunter2@h/db")
    assert "hunter2" not in repr(settings)


def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_DIM", "")
    assert Settings(_env_file=None).embedding_dim == 1536  # pyright: ignore[reportCallIssue]


def test_database_url_is_hidden_in_test_output() -> None:
    from tests.conftest import SecretUrl

    url = SecretUrl("postgresql://u:hunter2@h/db")
    assert "hunter2" not in repr(url)
    assert url == "postgresql://u:hunter2@h/db"
    assert f"{url}" == "postgresql://u:hunter2@h/db"
