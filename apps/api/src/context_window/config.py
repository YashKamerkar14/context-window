"""Settings read from the environment and, for local development, the repo's .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator
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

    # Embeddings. The model must produce `embedding_dim` dimensions and have a price.
    embedding_provider: str = "openrouter"
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None

    # Ranking and merging (PLAN.md 6.3 and 6.4). Thresholds are cosine similarities and
    # depend on the embedding model. These were calibrated for text-embedding-3-small:
    # see evals/calibration/merge-thresholds.md, and re-run `cw calibrate` if it changes.
    max_stories: int = Field(default=10, ge=1)
    fact_check_pool: int = Field(default=14, ge=1)
    merge_threshold: float = Field(default=0.81, ge=0, le=1)
    merge_ask_threshold: float = Field(default=0.70, ge=0, le=1)
    suppress_threshold: float = Field(default=0.84, ge=0, le=1)

    # Review and publishing (PLAN.md 6.3). Manual locally: the run pauses for a decision.
    # Auto in production: it approves itself. A paused run auto-approves after the timeout.
    review_mode: Literal["manual", "auto"] = "manual"
    review_timeout_hours: float = Field(default=3.0, gt=0)
    resend_api_key: SecretStr | None = None
    resend_from: str | None = None
    briefing_email_to: str | None = None
    # The web app's revalidate endpoint (Phase 8). Skipped until both are set.
    web_origin: str | None = None
    revalidate_secret: SecretStr | None = None

    # API (PLAN.md 8). Write endpoints need ADMIN_TOKEN; the browser never sees it. Runs
    # are started and resumed through workflow_dispatch with a fine-grained token that
    # can only use Actions on this repository.
    admin_token: SecretStr | None = None
    github_dispatch_token: SecretStr | None = None
    github_repository: str = "YashKamerkar14/context-window"
    # Sent by Vercel Cron to GET /cron/daily-run (apps/api/vercel.json).
    cron_secret: SecretStr | None = None
    newsroom_workflow: str = "newsroom.yml"

    # Set by the newsroom workflow: the Actions run that executes this newsroom run.
    actions_run_url: str | None = None

    # Run limits (PLAN.md 6.5).
    max_items_per_beat: int = Field(default=40, ge=1, le=200)
    max_model_calls_per_run: int = Field(default=150, ge=1)
    max_cost_per_run_usd: float = Field(default=1.50, gt=0)

    # Tracing. LangSmith reads these from the process environment; see tracing.py.
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "context-window"
    langsmith_tracing: bool = True

    # Sources. The token goes only to the GitHub MCP server.
    github_token: SecretStr | None = None
    max_fetch_bytes: int = Field(default=2_000_000, ge=10_000)

    # Starter sources, watchlist and interests.
    settings_file: Path = REPO_ROOT / "config" / "settings.toml"

    @field_validator(
        "database_url",
        "database_url_direct",
        "model_api_key",
        "embedding_api_key",
        "langsmith_api_key",
        "github_token",
        "resend_api_key",
        "briefing_email_to",
        "admin_token",
        "github_dispatch_token",
        "cron_secret",
        mode="before",
    )
    @classmethod
    def _unquote(cls, value: object, info: ValidationInfo) -> object:
        """Undo what copying a whole `.env` line into a CI secret adds: surrounding
        whitespace, the variable's own `NAME =` prefix, and one pair of quotes. A `.env`
        file strips these itself; a raw environment variable keeps them literally."""
        if not isinstance(value, str):
            return value
        value = value.strip()
        name = (info.field_name or "").upper()
        if name and value.upper().startswith(name):
            rest = value[len(name) :].lstrip()
            if rest.startswith("="):
                value = rest[1:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        return value

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

    def require_embedding_model(self) -> str:
        name = self.embedding_model
        if not name:
            raise RuntimeError("Set EMBEDDING_MODEL in the environment.")
        if name not in self.model_prices:
            raise RuntimeError(
                f"MODEL_PRICES has no entry for {name!r}; the budget cannot price its calls."
            )
        return name

    def embedding_key(self) -> str:
        key = self.embedding_api_key or self.model_api_key
        if key is None:
            raise RuntimeError("Set EMBEDDING_API_KEY (or MODEL_API_KEY) in the environment.")
        return key.get_secret_value()

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
