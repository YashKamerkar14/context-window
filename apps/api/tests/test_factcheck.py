"""The fact-checker: evidence helpers, the quote check, and the fact_check_story subgraph
end to end against an in-memory page, with stub judge and rewriter."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
from collections.abc import Sequence
from typing import Any

import httpx
from context_window_mcp import web_fetch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db import models as db
from context_window.db.queries import create_run
from context_window.factcheck.evidence import fetch_url, passages, quote_found, trim
from context_window.factcheck.graph import build_fact_check_graph
from context_window.factcheck.judge import (
    WHY_ID,
    ClaimToCheck,
    JudgedClaim,
    Judgement,
    apply_quote_check,
)
from context_window.factcheck.rewrite import RewrittenStory
from context_window.graph.rank import save_stories
from context_window.graph.reporter.drafts import DraftClaim, DraftSentence
from context_window.ids import uuid7
from context_window.schemas.enums import (
    Beat,
    DropReason,
    EvidenceKind,
    RunTrigger,
    StoryStatus,
    Verdict,
)
from context_window.schemas.newsroom import Claim, SentenceClaims, Story
from tests.fakes import embed_text, fake_embed, newsroom_context, transport

PAGE = "https://lab.example/releases/1.2.14"
MISSING = "https://lab.example/gone"
TEXT = (
    "LangGraph 1.2.14 adds durable retries for tasks. "
    "It requires Python 3.12 or later. " + "Other notes about the release follow here. " * 20
)


# --- Evidence and the quote check ------------------------------------------------------


def test_arxiv_abstracts_are_checked_against_the_full_text() -> None:
    assert fetch_url("https://arxiv.org/abs/2610.10184") == "https://arxiv.org/html/2610.10184"
    assert fetch_url("https://arxiv.org/abs/2610.10184v2") == "https://arxiv.org/html/2610.10184v2"
    assert fetch_url(PAGE) == PAGE


def test_quote_check_ignores_case_whitespace_and_typography() -> None:
    curly_open, curly_close, en_dash = chr(0x201C), chr(0x201D), chr(0x2013)
    page = (
        "The model scores 48.57 on\n"
        f"the {curly_open}Decision Index{curly_close} {en_dash} a new benchmark."
    )
    assert quote_found('scores 48.57 on the "decision index" - a new', page)
    assert not quote_found("scores 48.75 on the", page)
    assert not quote_found("48.57", page)  # too short to be evidence


def test_supported_without_a_real_quote_becomes_unclear() -> None:
    judged = JudgedClaim(
        claim_id="c1", verdict="supported", evidence_quote="not on the page", reasoning="r"
    )
    assert apply_quote_check(judged, TEXT).verdict is Verdict.UNCLEAR
    judged = judged.model_copy(update={"evidence_quote": "It requires Python 3.12 or later."})
    assert apply_quote_check(judged, TEXT).verdict is Verdict.SUPPORTED
    # "Why it matters" is advice: it needs no quote.
    why = JudgedClaim(claim_id=WHY_ID, verdict="supported", evidence_quote="", reasoning="r")
    assert apply_quote_check(why, TEXT).verdict is Verdict.SUPPORTED


async def test_trim_keeps_the_passages_nearest_the_claims_in_page_order() -> None:
    filler = " ".join(f"unrelated{i} words about gardening" for i in range(400))
    text = f"Alpha model ships today. {filler} The benchmark score is 91 percent. {filler}"
    trimmed = await trim(text, ["The benchmark score is 91 percent"], fake_embed, budget=3000)
    assert "benchmark score is 91 percent" in trimmed
    assert len(trimmed) <= 3000 + 20
    assert all(len(p) <= 1200 for p in passages(text))


# --- The subgraph ----------------------------------------------------------------------


def _page_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == PAGE:
        return httpx.Response(
            200,
            text=f"<html><body><article><p>{TEXT}</p></article></body></html>",
            headers={"content-type": "text/html"},
        )
    return httpx.Response(404)


def _story(source: str = PAGE, planted: str = "1.2.14 requires Python 3.10 or later.") -> Story:
    sentences = [
        SentenceClaims(sentence="LangGraph 1.2.14 adds durable retries.", claim_ids=["c1"]),
        SentenceClaims(sentence="It needs a recent Python.", claim_ids=["c2"]),
    ]
    return Story(
        id=str(uuid7()),
        beat=Beat.RELEASES,
        headline="LangGraph 1.2.14 adds durable retries",
        summary=" ".join(s.sentence for s in sentences),
        why_it_matters="Agents can retry failed tasks.",
        claims=[
            Claim(id="c1", text="1.2.14 adds durable retries for tasks.", source_url=source),
            Claim(id="c2", text=planted, source_url=source),
        ],
        sentence_claim_map=sentences,
        source_urls=[source],
        canonical_keys=["gh:langchain-ai/langgraph@1.2.14"],
        score=0.5,
    )


class PageJudge:
    """Supports a claim when its text is on the page; otherwise says unsupported."""

    def __init__(self, why_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.why_ok = why_ok

    async def __call__(self, source: str, claims: Sequence[ClaimToCheck]) -> list[Judgement]:
        self.calls.append([c.id for c in claims])
        answers: list[Judgement] = []
        for claim in claims:
            if claim.id == WHY_ID:
                ok = self.why_ok or claim.text != "Agents can retry failed tasks."
                answers.append(
                    Judgement(
                        claim.id, Verdict.SUPPORTED if ok else Verdict.UNSUPPORTED, None, "why"
                    )
                )
            elif claim.text.removeprefix("1.2.14 ").rstrip(".").lower() in source.lower():
                answers.append(Judgement(claim.id, Verdict.SUPPORTED, claim.text, "on the page"))
            else:
                answers.append(
                    Judgement(
                        claim.id,
                        Verdict.UNSUPPORTED,
                        "It requires Python 3.12 or later.",
                        "different version",
                    )
                )
        return answers


def _rewriter(claims: list[tuple[str, str]], why: str = "Agents can retry failed tasks.") -> Any:
    calls: list[Story] = []

    async def rewrite(story: Story, checked: Sequence[Claim], why_ok: bool) -> RewrittenStory:
        calls.append(story)
        return RewrittenStory(
            headline=story.headline,
            summary=[DraftSentence(text=f"Claim {i}.", claim_ids=[i]) for i, _ in claims],
            why_it_matters=why,
            claims=[DraftClaim(id=i, text=t) for i, t in claims],
        )

    rewrite.calls = calls  # type: ignore[attr-defined]
    return rewrite


async def _check(session: AsyncSession, story: Story, **agents: Any) -> dict[str, Any]:
    run = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)
    await save_stories(
        session, run.id, run.run_date, [(story, embed_text(story.headline))], "test/embed"
    )
    transports = {"web_fetch": transport(web_fetch.create_server, _page_handler)}
    async with newsroom_context(session, transports, **agents) as ctx:
        return await build_fact_check_graph().ainvoke(
            {"run_id": str(run.id), "story": story}, context=ctx
        )


async def _row(session: AsyncSession, story: Story) -> db.Story:
    row = await session.get(db.Story, story.id)
    assert row is not None
    await session.refresh(row)
    return row


async def test_a_planted_false_claim_is_corrected_and_rechecked(session: AsyncSession) -> None:
    judge = PageJudge()
    fixed = [
        ("c1", "1.2.14 adds durable retries for tasks."),
        ("c2", "1.2.14 requires Python 3.12 or later."),
    ]
    rewrite = _rewriter(fixed)
    story = _story()
    output = await _check(
        session, story, judge_claims=judge, judge_unclear=judge, rewrite_story=rewrite
    )

    [verified] = output["verified"]
    assert output.get("dropped", []) == []
    assert [c.text for c in verified.claims] == [t for _, t in fixed]
    assert all(
        c.verdict is Verdict.SUPPORTED and c.evidence_kind is EvidenceKind.PAGE
        for c in verified.claims
    )
    assert verified.attempts == 1
    # First pass judged both claims and the "why"; the recheck judged only the changed claim.
    assert judge.calls == [["c1", "c2", WHY_ID], ["c2"]]
    row = await _row(session, story)
    assert row.status is StoryStatus.VERIFIED and row.summary == "Claim c1. Claim c2."
    claims = (await session.scalars(select(db.Claim).where(db.Claim.story_id == row.id))).all()
    assert {c.verdict for c in claims} == {Verdict.SUPPORTED} and all(c.checked_at for c in claims)


async def test_a_false_claim_that_survives_the_rewrite_is_dropped(session: AsyncSession) -> None:
    judge = PageJudge()
    unchanged = [
        ("c1", "1.2.14 adds durable retries for tasks."),
        ("c2", "1.2.14 requires Python 3.10 or later."),
    ]
    story = _story()
    output = await _check(
        session, story, judge_claims=judge, judge_unclear=judge, rewrite_story=_rewriter(unchanged)
    )
    [dropped] = output["dropped"]
    assert dropped.reason is DropReason.UNSUPPORTED and output.get("verified", []) == []
    assert (await _row(session, story)).drop_reason is DropReason.UNSUPPORTED
    assert output["stats"].claims_unsupported == 1


async def test_a_rewrite_may_not_add_claims(session: AsyncSession) -> None:
    judge = PageJudge()
    adds = [
        ("c1", "1.2.14 adds durable retries for tasks."),
        ("c9", "1.2.14 is the fastest release ever."),
    ]
    output = await _check(
        session, _story(), judge_claims=judge, judge_unclear=judge, rewrite_story=_rewriter(adds)
    )
    [dropped] = output["dropped"]
    assert dropped.reason is DropReason.UNSUPPORTED
    assert any("invalid rewrite" in e.message for e in output["stats"].errors)


async def test_an_unreachable_source_is_dropped_without_a_rewrite(session: AsyncSession) -> None:
    judge = PageJudge()
    rewrite = _rewriter([])
    output = await _check(
        session,
        _story(source=MISSING),
        judge_claims=judge,
        judge_unclear=judge,
        rewrite_story=rewrite,
    )
    [dropped] = output["dropped"]
    assert dropped.reason is DropReason.FETCH_FAILED
    assert judge.calls == [] and rewrite.calls == []


async def test_a_flagged_why_it_matters_is_rewritten(session: AsyncSession) -> None:
    judge = PageJudge(why_ok=False)
    true_claims = [
        ("c1", "1.2.14 adds durable retries for tasks."),
        ("c2", "1.2.14 requires Python 3.12 or later."),
    ]
    story = _story(planted="1.2.14 requires Python 3.12 or later.")
    rewrite = _rewriter(true_claims, why="Teams on Python 3.12 can adopt it.")
    output = await _check(
        session, story, judge_claims=judge, judge_unclear=judge, rewrite_story=rewrite
    )
    [verified] = output["verified"]
    assert verified.why_it_matters == "Teams on Python 3.12 can adopt it."
    assert judge.calls[-1] == [WHY_ID]  # the claims were unchanged; only the new line is checked


async def test_unclear_claims_go_to_the_strong_judge(session: AsyncSession) -> None:
    async def unsure(source: str, claims: Sequence[ClaimToCheck]) -> list[Judgement]:
        return [
            Judgement(
                c.id, Verdict.SUPPORTED if c.id == WHY_ID else Verdict.UNCLEAR, None, "unsure"
            )
            for c in claims
        ]

    strong = PageJudge()
    story = _story(planted="1.2.14 requires Python 3.12 or later.")
    output = await _check(
        session, story, judge_claims=unsure, judge_unclear=strong, rewrite_story=_rewriter([])
    )
    assert len(output["verified"]) == 1
    assert strong.calls == [["c1", "c2"]]
