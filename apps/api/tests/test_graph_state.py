"""The state reducers under parallel writes, and a checkpoint round trip in strict mode."""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import os

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from context_window.graph.state import NewsroomState
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import RunStats, StoryCandidate
from tests.test_newsroom_schemas import candidate


def _branch(name: str, cost: float):
    def node(state: NewsroomState) -> NewsroomState:
        return {
            "candidates": [StoryCandidate.model_validate(candidate(headline=name))],
            "stats": RunStats(items_fetched_by_beat={Beat.RELEASES: 1}, cost_usd=cost),
        }

    return node


async def test_parallel_branches_append_and_merge_and_survive_a_checkpoint() -> None:
    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
    builder = StateGraph(NewsroomState)
    builder.add_node("a", _branch("a", 0.25))
    builder.add_node("b", _branch("b", 0.5))
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    graph = builder.compile(checkpointer=InMemorySaver())

    config: RunnableConfig = {"configurable": {"thread_id": "t1"}}
    await graph.ainvoke({"run_id": "r1"}, config)

    # Read back from the checkpoint, which goes through the strict deserializer.
    state = (await graph.aget_state(config)).values
    assert sorted(c.headline for c in state["candidates"]) == ["a", "b"]
    assert all(isinstance(c, StoryCandidate) for c in state["candidates"])
    assert state["stats"].items_fetched_by_beat == {Beat.RELEASES: 2}
    assert state["stats"].cost_usd == 0.75
