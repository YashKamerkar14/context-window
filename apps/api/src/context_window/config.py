"""Settings read from the environment and, for local development, the repo's .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/context_window/config.py -> parents[4] is the repository root (editable installs only).
REPO_ROOT = Path(__file__).resolve().parents[4]


class ModelPrice(BaseModel):
    """USD per million tokens, as listed by the provider."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    input: float = Field(alias="in", ge=0)
    output: float = Field(alias="out", ge=0)


ModelTier = Literal["fast", "strong"]


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

    # Models. Names are the provider's, e.g. `anthropic/claude-haiku-5.5` on OpenRouter.
    model_provider: str = "openrouter"
    model_fast: str | None = None
    model_strong: str | None = None
    model_api_key: SecretStr | None = None
    # JSON in the environment: {"<model>": {"in": 0.10, "out": 0.50}}.
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict[str, ModelPrice])

    # Run limits (PLAN.md 6.5).
    max_items_per_beat: int = Field(default=40, ge=1, le=200)
    max_model_calls_per_run: int = Field(default=150, ge=1)
    max_cost_per_run_usd: float = Field(default=1.50, gt=0)

    # Tracing. LangSmith reads these from the process environment; see export_tracing_env.
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "context-window"
    langsmith_tracing: bool = True

    # Sources. The token goes only to the GitHub MCP server.
    github_token: SecretStr | None = None
    max_fetch_bytes: int = Field(default=2_000_000, ge=10_000)

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

    def model_name(self, tier: ModelTier) -> str:
        name = self.model_fast if tier == "fast" else self.model_strong
        if not name:
            raise RuntimeError(f"Set MODEL_{tier.upper()} in the environment.")
        if name not in self.model_prices:
            raise RuntimeError(
                f"MODEL_PRICES has no entry for {name!r}; the budget cannot price its calls."
            )
        return name

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
