import datetime as dt
from typing import Any

import pytest

from context_window.graph.briefing import default_intro, order_stories, render_markdown
from context_window.graph.rank import link_candidates, merge_group, select_pool
from context_window.graph.scoring import (
    InterestVector,
    authority_score,
    cross_source,
    independent_domains,
    interest_match,
    recency,
    score,
)
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import Briefing, RunStats, Story, StoryCandidate
from tests.fakes import embed_text
from tests.test_newsroom_schemas import candidate

NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.UTC)


def _candidate(
    beat: Beat, headline: str, keys: list[str], urls: list[str] | None = None
) -> StoryCandidate:
    sentence = f"{headline}."
    return StoryCandidate.model_validate(
        candidate(
            beat=beat,
            headline=headline,
            summary=sentence,
            sentence_claim_map=[{"sentence": sentence, "claim_ids": ["c1"]}],
            claims=[{"id": "c1", "text": sentence, "source_url": (urls or ["u"])[0]}],
            canonical_keys=keys,
            source_urls=urls or [f"https://{keys[0]}.example/"],
            is_breaking_change=False,
        )
    )


def _story(beat: Beat, story_score: float, **overrides: Any) -> Story:
    base = _candidate(beat, f"{beat} story {story_score}", [f"k{beat}{story_score}"])
    return Story(**(base.model_dump() | overrides), id=f"{beat}-{story_score}", score=story_score)


# --- Scoring (PLAN.md 6.4) -------------------------------------------------------------


def test_interest_match_rescales_cosine_and_scales_by_weight() -> None:
    vector = [1.0, 0.0]
    same = InterestVector("a", 2.0, [1.0, 0.0])
    assert interest_match(vector, [same]) == pytest.approx(1.0)
    half_weight = InterestVector("a", 1.0, [1.0, 0.0])
    assert interest_match(vector, [half_weight]) == pytest.approx(0.5)
    # cos 0.25 is the floor: anything at or below it scores 0.
    unrelated = InterestVector("b", 2.0, [0.25, (1 - 0.25**2) ** 0.5])
    assert interest_match(vector, [unrelated]) == pytest.approx(0.0)
    assert interest_match(vector, []) == 0.0


def test_components() -> None:
    assert authority_score(1) == 0.0 and authority_score(5) == 1.0 and authority_score(3) == 0.5
    assert recency(NOW - dt.timedelta(hours=10), NOW) == 1.0
    assert recency(NOW - dt.timedelta(hours=48), NOW) == pytest.approx(0.5)
    assert recency(NOW - dt.timedelta(hours=80), NOW) == 0.0
    assert recency(None, NOW) == 0.5
    assert cross_source(Beat.RELEASES, ["https://a.example"]) == 0.5
    assert cross_source(Beat.INDUSTRY, ["https://openai.com/x"]) == 0.0
    hn = "https://news.ycombinator.com/item?id=1"
    assert independent_domains(["https://openai.com/x", hn]) == 1  # HN threads don't count
    two = ["https://openai.com/x", "https://www.theverge.com/y"]
    assert cross_source(Beat.COMMUNITY, two) == 0.5
    assert cross_source(Beat.COMMUNITY, [*two, "https://a.b.example.org/z", "https://c.io"]) == 1.0
    assert score(interest=1, authority=1, fresh=1, cross=1) == 1.0
    assert score(interest=0.5, authority=0, fresh=0, cross=0) == 0.2


# --- Merging ---------------------------------------------------------------------------


def test_shared_keys_and_close_vectors_merge_but_releases_never_merge_by_embedding() -> None:
    candidates = [
        _candidate(
            Beat.INDUSTRY, "OpenAI launches GPT-6 Luna for long context", ["openai.com/luna"]
        ),
        _candidate(
            Beat.COMMUNITY, "OpenAI launches GPT-6 Luna for long context", ["blog.example/luna"]
        ),
        _candidate(Beat.COMMUNITY, "Thread on the Luna launch", ["openai.com/luna"]),  # same key
        _candidate(
            Beat.RELEASES, "openai 3.26.0 adds Decisions", ["gh:openai/openai-python@v3.26.0"]
        ),
        _candidate(
            Beat.RELEASES, "openai 3.26.1 adds Decisions", ["gh:openai/openai-python@v3.26.1"]
        ),
        _candidate(Beat.RESEARCH, "A benchmark for retrieval", ["arxiv:2610.00001"]),
    ]
    vectors = [embed_text(f"{c.headline}. {c.summary}") for c in candidates]
    links, borderline = link_candidates(
        candidates, vectors, merge_threshold=0.85, ask_threshold=0.75
    )
    groups = sorted(sorted(g) for g in links.groups())
    assert groups == [[0, 1, 2], [3], [4], [5]]
    assert all({p.first, p.second} != {3, 4} for p in borderline)


def test_merge_keeps_the_best_sourced_text_and_every_source() -> None:
    community = _candidate(
        Beat.COMMUNITY,
        "Thread",
        ["blog.example/luna"],
        ["https://blog.example/luna", "https://news.ycombinator.com/item?id=1"],
    )
    industry = _candidate(
        Beat.INDUSTRY, "OpenAI launches Luna", ["openai.com/luna"], ["https://openai.com/luna"]
    )
    merged = merge_group([community, industry], {"openai.com/luna": 5, "blog.example/luna": 2})
    assert merged.headline == "OpenAI launches Luna" and merged.beat is Beat.INDUSTRY
    assert merged.source_urls == [
        "https://openai.com/luna",
        "https://blog.example/luna",
        "https://news.ycombinator.com/item?id=1",
    ]
    assert merged.canonical_keys == ["openai.com/luna", "blog.example/luna"]
    assert cross_source(merged.beat, merged.source_urls) == 0.5  # two independent domains


def test_pool_has_one_to_four_per_beat() -> None:
    stories = [_story(Beat.INDUSTRY, 0.9 - i / 100) for i in range(8)]
    stories += [_story(Beat.RESEARCH, 0.2), _story(Beat.COMMUNITY, 0.1)]
    pool = select_pool(stories, 6)
    beats = [s.beat for s in pool]
    assert beats.count(Beat.INDUSTRY) == 4
    assert beats.count(Beat.RESEARCH) == 1 and beats.count(Beat.COMMUNITY) == 1
    assert [s.score for s in pool] == sorted((s.score for s in pool), reverse=True)


# --- Briefing --------------------------------------------------------------------------


def test_briefing_puts_breaking_releases_first_and_flags_light_days() -> None:
    breaking = _story(Beat.RELEASES, 0.3, is_breaking_change=True)
    stories = [_story(Beat.INDUSTRY, 0.9), breaking, _story(Beat.RESEARCH, 0.5)]
    ordered = order_stories(stories, limit=2)
    assert ordered == [breaking, stories[0]]
    assert default_intro(ordered, light_day=True).startswith("A light day: 2 stories")
    briefing = Briefing(
        date=NOW.date(), intro="Intro.", stories=ordered, light_day=True, stats=RunStats()
    )
    body = render_markdown(briefing)
    assert body.startswith("# Context Window, 2026-10-09\n\nIntro.\n")
    assert "_A light day" in body and "(breaking change)" in body
    assert "**Why it matters:**" in body


async def test_check_draft_reverts_an_unsupported_intro() -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from context_window.graph import briefing

    stories = [_story(Beat.INDUSTRY, 0.9)]
    draft = Briefing(
        date=NOW.date(),
        intro="It also cures cancer.",
        stories=stories,
        light_day=True,
        stats=RunStats(),
    )
    saved: list[str] = []

    async def reject(intro: str, given: object) -> bool:
        return False

    async def fake_save(ctx: object, run_id: object, b: Briefing, body: str) -> bool:
        saved.append(body)
        return True

    @asynccontextmanager
    async def nothing():  # pyright: ignore[reportUnusedFunction]
        yield None

    runtime = SimpleNamespace(context=SimpleNamespace(agents=SimpleNamespace(check_draft=reject)))
    original = briefing.save_draft
    briefing.save_draft = fake_save  # pyright: ignore[reportAttributeAccessIssue]
    try:
        out = await briefing.check_draft(
            {
                "run_id": "01a11d19-0aaa-7c8c-b33a-cdb085ba6fbe",
                "run_date": NOW.date(),
                "draft": draft,
            },
            runtime,  # pyright: ignore[reportArgumentType]
        )
    finally:
        briefing.save_draft = original
    reverted = out.get("draft")
    assert reverted is not None and reverted.intro == default_intro(stories, True)
    assert saved and "cures cancer" not in saved[0]
