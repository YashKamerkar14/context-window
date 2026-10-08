"""The newsroom graph.

Phase 4 wires one path: the run's assignments fan out to a reporter each (`Send`), and
their candidates and stats are merged by the state reducers. The editor, merge and rank,
fact-checking, writing, review and publishing nodes join in later phases.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.graph import build_reporter_graph
from context_window.graph.state import NewsroomState


def assign_reporters(state: NewsroomState) -> list[Send]:
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("the run's input must include run_id")
    return [
        Send("reporter", {"run_id": run_id, "assignment": assignment})
        for assignment in state.get("assignments", [])
    ]


def build_newsroom_graph() -> StateGraph[NewsroomState, NewsroomContext]:
    """The uncompiled graph; the caller compiles it with its checkpointer."""
    builder = StateGraph(NewsroomState, context_schema=NewsroomContext)
    builder.add_node("reporter", build_reporter_graph())
    builder.add_conditional_edges(START, assign_reporters, ["reporter"])
    builder.add_edge("reporter", END)
    return builder
