"""The newsroom graph.

plan_coverage -> one reporter per assignment, in parallel (`Send`) -> merge_and_rank ->
one fact_check_story per pooled story, in parallel -> write_briefing -> check_draft ->
human_review (pauses in manual mode) -> publish -> update_memory. A rejected review ends
the run.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from context_window.factcheck.graph import build_fact_check_graph
from context_window.graph.briefing import check_draft, write_briefing
from context_window.graph.context import NewsroomContext
from context_window.graph.editor import plan_coverage
from context_window.graph.publish import publish, update_memory
from context_window.graph.rank import merge_and_rank
from context_window.graph.reporter.graph import build_reporter_graph
from context_window.graph.review import after_review, human_review
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


def assign_fact_checks(state: NewsroomState) -> list[Send] | str:
    stories = state.get("stories", [])
    if not stories:
        return "write_briefing"
    return [Send("fact_check", {"run_id": state["run_id"], "story": story}) for story in stories]


def build_newsroom_graph() -> StateGraph[NewsroomState, NewsroomContext]:
    """The uncompiled graph; the caller compiles it with its checkpointer."""
    builder = StateGraph(NewsroomState, context_schema=NewsroomContext)
    builder.add_node("plan_coverage", plan_coverage)
    builder.add_node("reporter", build_reporter_graph())
    builder.add_node("merge_and_rank", merge_and_rank)
    builder.add_node("fact_check", build_fact_check_graph())
    builder.add_node("write_briefing", write_briefing)
    builder.add_node("check_draft", check_draft)
    builder.add_node("human_review", human_review)
    builder.add_node("publish", publish)
    builder.add_node("update_memory", update_memory)
    builder.add_edge(START, "plan_coverage")
    builder.add_conditional_edges("plan_coverage", assign_reporters, ["reporter", "merge_and_rank"])
    builder.add_edge("reporter", "merge_and_rank")
    builder.add_conditional_edges(
        "merge_and_rank", assign_fact_checks, ["fact_check", "write_briefing"]
    )
    builder.add_edge("fact_check", "write_briefing")
    builder.add_edge("write_briefing", "check_draft")
    builder.add_edge("check_draft", "human_review")
    builder.add_conditional_edges("human_review", after_review, ["publish", END])
    builder.add_edge("publish", "update_memory")
    builder.add_edge("update_memory", END)
    return builder
