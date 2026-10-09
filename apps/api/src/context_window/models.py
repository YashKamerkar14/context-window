"""Chat models for the agents, built from settings so provider and model are configuration.

Every model carries a `BudgetCallback`, so no call can bypass the run's caps.
"""

import ssl
from typing import Any, cast

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from context_window.budget import BudgetCallback
from context_window.config import ModelTier, Settings

# Provider-specific options. On OpenRouter, `require_parameters` routes only to providers
# that support every parameter sent, so a structured-output request never lands on one
# that would ignore the schema. That is also why no `temperature` is sent: recent models
# (e.g. Claude Haiku 5.5) accept none, and requiring it would match no provider at all.
PROVIDER_OPTIONS: dict[str, dict[str, Any]] = {
    "openrouter": {"openrouter_provider": {"require_parameters": True}},
}


def chat_model(settings: Settings, tier: ModelTier) -> BaseChatModel:
    options: dict[str, Any] = dict(PROVIDER_OPTIONS.get(settings.model_provider, {}))
    if settings.model_api_key is not None:
        options["api_key"] = settings.model_api_key.get_secret_value()
    # init_chat_model is annotated loosely; every chat provider returns a BaseChatModel.
    model = init_chat_model(  # pyright: ignore[reportUnknownVariableType]
        settings.model_name(tier),
        model_provider=settings.model_provider,
        max_retries=2,
        callbacks=[BudgetCallback()],
        **options,
    )
    return cast(BaseChatModel, model)


# Network failures mid-response (a dropped or corrupted TLS connection) that the
# provider's SDK treats as permanent and does not retry itself.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ssl.SSLError,
    ConnectionError,
)


def with_network_retry[I, O](runnable: Runnable[I, O]) -> Runnable[I, O]:
    """Retry a model call up to three times on network failures, with backoff. Each
    attempt is a separate call for the Budget."""
    return runnable.with_retry(
        retry_if_exception_type=NETWORK_ERRORS,
        wait_exponential_jitter=True,
        stop_after_attempt=3,
    )
