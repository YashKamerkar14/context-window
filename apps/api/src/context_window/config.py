"""Settings read from the environment and, for local development, the repo's .env file."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/context_window/config.py -> parents[4] is the repository root (editable installs only).
REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Real environment variables always win over the file.
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # App
    api_host: str = "127.0.0.1"
    port: int = 8000
    worker_heartbeat_seconds: float = Field(default=30.0, gt=0)
    briefing_tz: str = "UTC"

    # Database. Pooled for the API, direct for migrations, the worker and the checkpointer.
    database_url: SecretStr | None = None
    database_url_direct: SecretStr | None = None
    embedding_dim: int = Field(default=1536, ge=1, le=16000)

    # Starter sources, watchlist and interests.
    settings_file: Path = REPO_ROOT / "config" / "settings.toml"

    @field_validator("briefing_tz")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value

    def require_direct_url(self) -> str:
        """The non-pooled URL, falling back to the pooled one when only that is set."""
        url = self.database_url_direct or self.database_url
        if url is None:
            raise RuntimeError("Set DATABASE_URL_DIRECT (or DATABASE_URL) in the environment.")
        return url.get_secret_value()

    def require_pooled_url(self) -> str:
        url = self.database_url or self.database_url_direct
        if url is None:
            raise RuntimeError("Set DATABASE_URL (or DATABASE_URL_DIRECT) in the environment.")
        return url.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
