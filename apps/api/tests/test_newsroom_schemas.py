import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from context_window.graph.state import merge_stats
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import RunError, RunStats, StoryCandidate


def candidate(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "beat": Beat.RELEASES,
        "headline": "LangGraph 1.2.14 adds durable retries",
        "summary": "LangGraph 1.2.14 adds durable retries. It drops Python 3.11.",
        "why_it_matters": "Agents that crash mid-run can now resume without lost work.",
        "claims": [
            {"id": "c1", "text": "Version 1.2.14 adds durable retries.", "source_url": "u"},
            {"id": "c2", "text": "Version 1.2.14 drops Python 3.11.", "source_url": "u"},
        ],
        "sentence_claim_map": [
            {"sentence": "LangGraph 1.2.14 adds durable retries.", "claim_ids": ["c1"]},
            {"sentence": "It drops Python 3.11.", "claim_ids": ["c2"]},
        ],
        "source_urls": ["https://github.com/langchain-ai/langgraph/releases/tag/1.2.14"],
        "canonical_keys": ["gh:langchain-ai/langgraph@1.2.14"],
        "published_at": dt.datetime(2026, 10, 6, tzinfo=dt.UTC),
        "is_breaking_change": True,
    }
    return data | overrides


def test_valid_candidate() -> None:
    story = StoryCandidate.model_validate(candidate())
    assert [c.id for c in story.claims] == ["c1", "c2"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"sentence_claim_map": [{"sentence": "x", "claim_ids": ["c1", "c9"]}]},
            "unknown claims",
        ),
        (
            {
                "summary": "LangGraph 1.2.14 adds durable retries.",
                "sentence_claim_map": [
                    {"sentence": "LangGraph 1.2.14 adds durable retries.", "claim_ids": ["c1"]}
                ],
            },
            "not cited",
        ),
        ({"summary": "Something else entirely."}, "cover the summary exactly"),
        ({"beat": Beat.RESEARCH}, "releases beat only"),
        (
            {"claims": [{"id": "c1", "text": "t", "source_url": "u"}] * 2},
            "unique",
        ),
    ],
)
def test_invalid_candidates(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        StoryCandidate.model_validate(candidate(**overrides))


def test_at_most_four_claims_and_two_sentences() -> None:
    claims = [{"id": f"c{i}", "text": "t", "source_url": "u"} for i in range(5)]
    with pytest.raises(ValidationError, match="at most 4"):
        StoryCandidate.model_validate(candidate(claims=claims))
    three = [{"sentence": "a.", "claim_ids": ["c1"]}] * 3
    with pytest.raises(ValidationError, match="at most 2"):
        StoryCandidate.model_validate(candidate(sentence_claim_map=three))


def test_run_stats_merge_sums_counts_and_concatenates_errors() -> None:
    a = RunStats(
        items_fetched_by_beat={Beat.RELEASES: 3},
        model_calls=2,
        cost_usd=0.01,
        errors=[RunError(node="reporter", message="a")],
        timings_ms={"fetch": 100},
    )
    b = RunStats(
        items_fetched_by_beat={Beat.RELEASES: 2, Beat.RESEARCH: 5},
        model_calls=1,
        cost_usd=0.02,
        errors=[RunError(node="reporter", message="b")],
        timings_ms={"fetch": 50, "summarise": 10},
    )
    merged = a.merge(b)
    assert merged.items_fetched_by_beat == {Beat.RELEASES: 5, Beat.RESEARCH: 5}
    assert merged.model_calls == 3
    assert merged.cost_usd == pytest.approx(0.03)
    assert [e.message for e in merged.errors] == ["a", "b"]
    assert merged.timings_ms == {"fetch": 150, "summarise": 10}
    # The inputs are untouched.
    assert a.model_calls == 2 and b.timings_ms == {"fetch": 50, "summarise": 10}


def test_merge_stats_reducer_handles_missing_sides() -> None:
    stats = RunStats(model_calls=1)
    assert merge_stats(None, stats) is stats
    assert merge_stats(stats, None) is stats
    assert merge_stats(None, None) == RunStats()
