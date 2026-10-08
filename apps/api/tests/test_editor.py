from context_window.graph.editor import DEFAULT_FOCUS, build_assignments, render_brief
from context_window.graph.reporter.community import DEFAULT_KEYWORDS
from context_window.schemas.agents import BeatFocus, CoverageBrief, CoveragePlan
from context_window.schemas.enums import Beat
from tests.test_newsroom_schemas import dt


def test_assignments_use_the_plan_and_fall_back_to_defaults() -> None:
    plan = CoveragePlan(
        assignments=[
            BeatFocus(beat=Beat.RESEARCH, focus="  Agent memory   papers. ", keywords=["ignored"]),
            BeatFocus(beat=Beat.COMMUNITY, focus="", keywords=["Claude", "vLLM", " "]),
        ]
    )
    research, releases, community = build_assignments(
        [Beat.RESEARCH, Beat.RELEASES, Beat.COMMUNITY], plan, max_items=12
    )
    assert research.focus == "Agent memory papers." and research.keywords == []
    assert releases.focus == DEFAULT_FOCUS[Beat.RELEASES]
    assert community.focus == DEFAULT_FOCUS[Beat.COMMUNITY]
    # The editor's keywords add to the defaults; duplicates and blanks are dropped.
    assert community.keywords[: len(DEFAULT_KEYWORDS)] == list(DEFAULT_KEYWORDS)
    assert community.keywords[len(DEFAULT_KEYWORDS) :] == ["vLLM"]
    assert {a.max_items for a in (research, releases, community)} == {12}


def test_no_plan_means_default_focus_for_every_beat() -> None:
    assignments = build_assignments(list(Beat), None, max_items=40)
    assert [a.focus for a in assignments] == [DEFAULT_FOCUS[b] for b in Beat]


def test_brief_lists_everything_the_editor_needs() -> None:
    text = render_brief(
        CoverageBrief(
            run_date=dt.date(2026, 10, 9),
            beats=[Beat.RELEASES, Beat.COMMUNITY],
            watchlist=["langchain-ai/langgraph"],
            interests=["MCP: the Model Context Protocol"],
            yesterday=[],
        )
    )
    assert "Beats: releases, community" in text
    assert "langchain-ai/langgraph" in text and "- MCP: the Model Context Protocol" in text
    assert "(no briefing yet)" in text
