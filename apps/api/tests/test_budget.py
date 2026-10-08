import asyncio
from collections.abc import Iterator

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from context_window.budget import (
    Budget,
    BudgetCallback,
    BudgetExceeded,
    UnpricedModelError,
    current_budget,
    use_budget,
)
from context_window.config import ModelPrice

PRICES = {"test/fast": ModelPrice.model_validate({"in": 0.10, "out": 0.50})}


def _budget(*, max_cost: float = 1.0, max_calls: int = 10) -> Budget:
    return Budget(prices=PRICES, max_cost_usd=max_cost, max_calls=max_calls)


class FakeModel(GenericFakeChatModel):
    model: str = "test/fast"


def _model(replies: int, *, cost: float | None = 0.0001) -> FakeModel:
    def messages() -> Iterator[AIMessage]:
        for _ in range(replies):
            yield AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
                response_metadata={} if cost is None else {"cost": cost},
            )

    return FakeModel(messages=messages(), callbacks=[BudgetCallback()])


def test_prices_tokens_per_million() -> None:
    budget = _budget()
    assert budget.record("test/fast", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert budget.record("test/fast", 2000, 400, reported_cost=0.0004) == pytest.approx(0.0004)
    stats = budget.stats()
    assert (stats.tokens_in, stats.tokens_out) == (1_002_000, 1_000_400)
    assert stats.cost_usd == pytest.approx(0.6004)
    assert budget.reported_cost_usd == pytest.approx(0.0004)


def test_unpriced_model_is_an_error() -> None:
    with pytest.raises(UnpricedModelError):
        _budget().record("test/unknown", 1, 1)


def test_call_cap() -> None:
    budget = _budget(max_calls=2)
    budget.start_call()
    budget.start_call()
    with pytest.raises(BudgetExceeded, match="call limit"):
        budget.start_call()


def test_cost_cap_stops_the_next_call() -> None:
    budget = _budget(max_cost=0.001)
    budget.start_call()
    budget.record("test/fast", 10_000, 0)  # exactly $0.001
    with pytest.raises(BudgetExceeded, match="cost limit"):
        budget.start_call()


async def test_callback_charges_the_current_budget() -> None:
    model = _model(2)
    with use_budget(_budget()) as budget:
        await model.ainvoke("hi")
        await model.ainvoke("hi again")
    assert current_budget() is None
    assert budget.calls_started == 2
    usage = budget.by_model["test/fast"]
    assert (usage.calls, usage.tokens_in, usage.tokens_out) == (2, 2000, 400)
    assert usage.cost_usd == pytest.approx(2 * (1000 * 0.10 + 200 * 0.50) / 1e6)
    assert usage.reported_cost_usd == pytest.approx(0.0002)


async def test_callback_blocks_calls_past_the_cap() -> None:
    model = _model(2)
    with use_budget(_budget(max_calls=1)) as budget:
        await model.ainvoke("hi")
        with pytest.raises(BudgetExceeded):
            await model.ainvoke("hi again")
    assert budget.by_model["test/fast"].calls == 1


async def test_parallel_tasks_share_one_budget() -> None:
    model = _model(5)
    with use_budget(_budget()) as budget:
        await asyncio.gather(*(model.ainvoke(str(i)) for i in range(5)))
    assert budget.by_model["test/fast"].calls == 5


async def test_without_a_budget_calls_are_not_tracked() -> None:
    assert (await _model(1).ainvoke("hi")).content == "ok"
