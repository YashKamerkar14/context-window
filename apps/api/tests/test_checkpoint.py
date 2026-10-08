"""The Postgres checkpointer against the test database, in the throwaway schema."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt

from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph

from context_window.graph.checkpoint import postgres_checkpointer
from context_window.graph.state import NewsroomState, NewsroomUpdate
from context_window.schemas.newsroom import RunStats, StoryCandidate
from tests.conftest import with_search_path
from tests.test_newsroom_schemas import candidate


def _report(state: NewsroomState) -> NewsroomUpdate:
    return {
        "candidates": [StoryCandidate.model_validate(candidate())],
        "stats": RunStats(model_calls=1),
    }


async def test_state_survives_a_postgres_checkpoint(
    database_url: str, migrated_schema: str
) -> None:
    url = with_search_path(database_url, migrated_schema)
    async with postgres_checkpointer(url, max_size=1) as saver:
        await saver.setup()
        builder = StateGraph(NewsroomState)
        builder.add_node("report", _report)
        builder.add_edge(START, "report")
        graph = builder.compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": "run-1"}}
        await graph.ainvoke({"run_id": "run-1", "run_date": dt.date(2026, 10, 9)}, config)

    # A fresh saver (a resumed job) reads the same state back.
    async with postgres_checkpointer(url, max_size=1) as saver:
        graph = builder.compile(checkpointer=saver)
        state = (await graph.aget_state(config)).values
    assert state["run_id"] == "run-1"
    assert [c.headline for c in state["candidates"]] == [candidate()["headline"]]
    assert state["stats"].model_calls == 1
