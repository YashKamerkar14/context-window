"""The run's model budget (PLAN.md 6.5).

Graph state cannot enforce a cost cap, because parallel branches cannot see each other's
writes. Instead one `Budget` per run lives in a context variable, which every branch
shares (asyncio tasks copy the context, and the Budget object is shared, not copied).
`BudgetCallback` is attached to every chat model: before a call it raises
`BudgetExceeded` when a cap is already reached, and after a call it records the tokens
priced with `MODEL_PRICES`.

The provider's own cost figure (OpenRouter returns one per call) is recorded alongside,
so the price table can be checked against what was actually charged.
"""

import threading
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from context_window.config import ModelPrice
from context_window.schemas.newsroom import RunStats


class BudgetExceeded(RuntimeError):
    """A run cap was reached. Nodes catch it, stop spending and keep what they have."""


class UnpricedModelError(RuntimeError):
    pass


@dataclass
class ModelUsage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    reported_cost_usd: float = 0.0


@dataclass
class Budget:
    prices: Mapping[str, ModelPrice]
    max_cost_usd: float
    max_calls: int
    calls_started: int = 0
    by_model: dict[str, ModelUsage] = field(default_factory=dict[str, ModelUsage])
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cost_usd(self) -> float:
        return sum(usage.cost_usd for usage in self.by_model.values())

    @property
    def reported_cost_usd(self) -> float:
        return sum(usage.reported_cost_usd for usage in self.by_model.values())

    def start_call(self) -> None:
        """Reserve one call, or raise if a cap is already reached."""
        with self._lock:
            if self.calls_started >= self.max_calls:
                raise BudgetExceeded(f"model call limit reached ({self.max_calls} calls)")
            if self.cost_usd >= self.max_cost_usd:
                raise BudgetExceeded(
                    f"cost limit reached (${self.cost_usd:.4f} of ${self.max_cost_usd:.2f})"
                )
            self.calls_started += 1

    def record(
        self, model: str, tokens_in: int, tokens_out: int, reported_cost: float | None = None
    ) -> float:
        """Record a finished call and return its cost from the price table."""
        price = self.prices.get(model)
        if price is None:
            raise UnpricedModelError(f"no price for model {model!r} in MODEL_PRICES")
        cost = (tokens_in * price.input + tokens_out * price.output) / 1_000_000
        with self._lock:
            usage = self.by_model.setdefault(model, ModelUsage())
            usage.calls += 1
            usage.tokens_in += tokens_in
            usage.tokens_out += tokens_out
            usage.cost_usd += cost
            usage.reported_cost_usd += reported_cost or 0.0
        return cost

    def stats(self) -> RunStats:
        usage = self.by_model.values()
        return RunStats(
            model_calls=self.calls_started,
            tokens_in=sum(u.tokens_in for u in usage),
            tokens_out=sum(u.tokens_out for u in usage),
            cost_usd=round(self.cost_usd, 6),
        )


_current: ContextVar[Budget | None] = ContextVar("context_window_budget", default=None)


def current_budget() -> Budget | None:
    return _current.get()


@contextmanager
def use_budget(budget: Budget) -> Generator[Budget]:
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


class BudgetCallback(AsyncCallbackHandler):
    """Charges every chat model call to the current run's Budget. Without one, does nothing."""

    raise_error = True  # BudgetExceeded must stop the call, not be logged and ignored

    def __init__(self) -> None:
        super().__init__()
        self._models: dict[uuid.UUID, str] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        budget = current_budget()
        if budget is None:
            return
        budget.start_call()
        # The requested name, which is the price table's key. The response may carry a
        # dated variant of it instead.
        model = (metadata or {}).get("ls_model_name")
        if isinstance(model, str):
            self._models[run_id] = model

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        model = self._models.pop(run_id, None)
        budget = current_budget()
        if budget is None or model is None:
            return
        for generation in (g for batch in response.generations for g in batch):
            if not isinstance(generation, ChatGeneration):
                continue
            message = generation.message
            if not isinstance(message, AIMessage) or message.usage_metadata is None:
                continue
            reported = message.response_metadata.get("cost")
            budget.record(
                model,
                message.usage_metadata["input_tokens"],
                message.usage_metadata["output_tokens"],
                float(reported) if isinstance(reported, int | float) else None,
            )

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._models.pop(run_id, None)
