"""The newsroom graph.

plan_coverage -> one reporter per assignment, in parallel (`Send`) -> merge_and_rank ->
write_briefing. Fact-checking, review and publishing join in later phases.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from context_window.graph.briefing import write_briefing
from context_window.graph.context import NewsroomContext
from context_window.graph.editor import plan_coverage
from context_window.graph.rank import merge_and_rank
from context_window.graph.reporter.graph import build_reporter_graph
from context_window.graph.state import NewsroomState


def assign_reporters(state: NewsroomState) -> list[Send] | str:
    run_id = state.get("run_id")
    if not run_id:
        raise ValueError("the run's input must include run_id")
    assignments = state.get("assignments", [])
    if not assignments:
        return "merge_and_rank"
    return [
        Send("reporter", {"run_id": run_id, "assignment": assignment}) for assignment in assignments
    ]


def build_newsroom_graph() -> StateGraph[NewsroomState, NewsroomContext]:
    """The uncompiled graph; the caller compiles it with its checkpointer."""
    builder = StateGraph(NewsroomState, context_schema=NewsroomContext)
    builder.add_node("plan_coverage", plan_coverage)
    builder.add_node("reporter", build_reporter_graph())
    builder.add_node("merge_and_rank", merge_and_rank)
    builder.add_node("write_briefing", write_briefing)
    builder.add_edge(START, "plan_coverage")
    builder.add_conditional_edges("plan_coverage", assign_reporters, ["reporter", "merge_and_rank"])
    builder.add_edge("reporter", "merge_and_rank")
    builder.add_edge("merge_and_rank", "write_briefing")
    builder.add_edge("write_briefing", END)
    return builder
