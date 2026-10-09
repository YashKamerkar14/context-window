"""The `fact_check_story` subgraph: check, at most one rewrite, then verify or drop.

check -> (passes, or rewritten already, or nothing a rewrite could fix) -> finish
      -> otherwise rewrite -> check (only the claims the rewrite changed) -> finish

- **check** groups the unchecked claims by source page, fetches each page once (falling
  back to the reporter's text), trims it to the passages that matter, and judges all of
  a page's claims in one fast-model call. Claims the fast judge finds `unclear` go to the
  strong model with more of the page. "Why it matters" is judged with the primary source.
- **Pass** means every claim is supported (so every summary sentence cites only supported
  claims) and "why it matters" states nothing the source lacks.
- **finish** emits the story to `verified` or `dropped`, with the reason, and saves the
  verdicts.

The input is one story; the output is appended to `verified`, `dropped` and `stats`.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import logging
import operator
import time
import uuid
from typing import Annotated, Literal, Required, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError
from sqlalchemy import delete, update

from context_window.budget import BudgetExceeded
from context_window.db import models as db
from context_window.factcheck.evidence import gather_evidence, trim
from context_window.factcheck.judge import WHY_ID, ClaimToCheck, Judgement
from context_window.graph.context import NewsroomContext
from context_window.graph.state import merge_stats
from context_window.schemas.enums import DropReason, StoryStatus, Verdict
from context_window.schemas.newsroom import (
    Claim,
    DroppedStory,
    RunError,
    RunStats,
    SentenceClaims,
    Story,
)

log = logging.getLogger(__name__)

ESCALATION_CHARS = 24_000  # about 6k tokens of the page for the strong model
MAX_REWRITES = 1
FETCH_FAILED = "could not fetch the source"


class FactCheckInput(TypedDict):
    run_id: str
    story: Story


class FactCheckOutput(TypedDict, total=False):
    verified: Annotated[list[Story], operator.add]
    dropped: Annotated[list[DroppedStory], operator.add]
    stats: Annotated[RunStats, merge_stats]


class FactCheckUpdate(TypedDict, total=False):
    story: Story
    why_ok: bool | None  # None: not judged yet
    rewrites: int
    fetch_failed: list[str]
    budget_hit: bool
    verified: Annotated[list[Story], operator.add]
    dropped: Annotated[list[DroppedStory], operator.add]
    stats: Annotated[RunStats, merge_stats]


class FactCheckState(TypedDict, total=False):
    run_id: Required[str]
    story: Required[Story]
    why_ok: bool | None
    rewrites: int
    fetch_failed: list[str]
    budget_hit: bool
    verified: Annotated[list[Story], operator.add]
    dropped: Annotated[list[DroppedStory], operator.add]
    stats: Annotated[RunStats, merge_stats]


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _apply(claim: Claim, judged: Judgement, evidence_url: str, kind: object) -> Claim:
    return claim.model_copy(
        update={
            "verdict": judged.verdict,
            "evidence_url": evidence_url,
            "evidence_quote": judged.quote,
            "evidence_kind": kind,
            "reasoning": judged.reasoning,
            "attempts": claim.attempts + 1,
        }
    )


def passes(story: Story, why_ok: bool | None) -> bool:
    return why_ok is True and all(c.verdict is Verdict.SUPPORTED for c in story.claims)


async def check(state: FactCheckState, runtime: Runtime[NewsroomContext]) -> FactCheckUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    run_id = uuid.UUID(state["run_id"])
    story = state["story"]
    why_ok = state.get("why_ok")
    failed = set(state.get("fetch_failed", []))
    claims = {c.id: c for c in story.claims}
    primary = story.claims[0].source_url
    sources = list(dict.fromkeys(c.source_url for c in story.claims if c.verdict is None))
    if why_ok is None and primary not in sources:
        sources.insert(0, primary)

    try:
        for source in sources:
            pending = [c for c in claims.values() if c.source_url == source and c.verdict is None]
            batch = [ClaimToCheck(c.id, c.text) for c in pending]
            if why_ok is None and source == primary:
                batch.append(ClaimToCheck(WHY_ID, story.why_it_matters))
            evidence = await gather_evidence(ctx, run_id, source)
            if evidence is None:
                failed.add(source)
                for c in pending:
                    claims[c.id] = c.model_copy(
                        update={"verdict": Verdict.UNCLEAR, "reasoning": FETCH_FAILED}
                    )
                if source == primary:
                    why_ok = True  # nothing to judge it against; the claims decide
                continue

            texts = [c.text for c in batch]
            source_text = await trim(evidence.text, texts, ctx.embed)
            judged = {j.claim_id: j for j in await ctx.agents.judge_claims(source_text, batch)}
            unclear = [
                c for c in batch if c.id != WHY_ID and judged[c.id].verdict is Verdict.UNCLEAR
            ]
            if unclear:
                wider = await trim(
                    evidence.text, [c.text for c in unclear], ctx.embed, ESCALATION_CHARS
                )
                for second in await ctx.agents.judge_unclear(wider, unclear):
                    judged[second.claim_id] = second
            for c in pending:
                claims[c.id] = _apply(c, judged[c.id], evidence.url, evidence.kind)
            if WHY_ID in judged:
                why_ok = judged[WHY_ID].verdict is not Verdict.UNSUPPORTED
    except BudgetExceeded as exc:
        return {
            "budget_hit": True,
            "stats": RunStats(
                errors=[RunError(node="fact_check", source="budget", message=str(exc))],
                timings_ms={"fact_check": _ms(started)},
            ),
        }

    checked = story.model_copy(update={"claims": [claims[c.id] for c in story.claims]})
    return {
        "story": checked,
        "why_ok": why_ok,
        "fetch_failed": sorted(failed),
        "stats": RunStats(timings_ms={"fact_check": _ms(started)}),
    }


def after_check(state: FactCheckState) -> Literal["rewrite", "finish"]:
    story = state["story"]
    if state.get("budget_hit") or passes(story, state.get("why_ok")):
        return "finish"
    if state.get("rewrites", 0) >= MAX_REWRITES:
        return "finish"
    # A rewrite cannot fix a source that could not be fetched.
    fixable = [
        c
        for c in story.claims
        if c.verdict is not Verdict.SUPPORTED and c.reasoning != FETCH_FAILED
    ]
    return "rewrite" if fixable or state.get("why_ok") is False else "finish"


async def rewrite(state: FactCheckState, runtime: Runtime[NewsroomContext]) -> FactCheckUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    story = state["story"]
    why_ok = state.get("why_ok") is True
    timing = {"fact_check.rewrite": _ms(started)}
    try:
        rewritten = await ctx.agents.rewrite_story(story, story.claims, why_ok)
    except BudgetExceeded as exc:
        error = RunError(node="fact_check.rewrite", source="budget", message=str(exc))
        return {"budget_hit": True, "rewrites": 1, "stats": RunStats(errors=[error])}
    except Exception as exc:  # the story is dropped as it stands
        log.exception("rewrite failed")
        error = RunError(node="fact_check.rewrite", message=f"{type(exc).__name__}: {exc}")
        return {"rewrites": 1, "stats": RunStats(errors=[error], timings_ms=timing)}

    original = {c.id: c for c in story.claims}
    claims: list[Claim] = []
    for draft in rewritten.claims:
        old = original.get(draft.id.strip())
        if old is None:
            continue  # a new claim would be a new fact
        text = _clean(draft.text)
        if text == old.text:
            claims.append(old)  # unchanged: keeps its verdict, good or bad
        else:
            claims.append(
                Claim(id=old.id, text=text, source_url=old.source_url, attempts=old.attempts)
            )
    kept = {c.id for c in claims}
    sentences = [
        SentenceClaims(sentence=_clean(s.text), claim_ids=[i.strip() for i in s.claim_ids])
        for s in rewritten.summary
    ]
    why = _clean(rewritten.why_it_matters)
    try:
        revised = Story.model_validate(
            story.model_dump()
            | {
                "headline": _clean(rewritten.headline),
                "summary": " ".join(s.sentence for s in sentences),
                "why_it_matters": why,
                "claims": [c.model_dump() for c in claims if c.id in kept],
                "sentence_claim_map": [s.model_dump() for s in sentences],
                "attempts": story.attempts + 1,
            }
        )
    except ValidationError as exc:
        problems = "; ".join(e["msg"] for e in exc.errors())
        error = RunError(
            node="fact_check.rewrite", source=story.id, message=f"invalid rewrite: {problems}"
        )
        return {"rewrites": 1, "stats": RunStats(errors=[error], timings_ms=timing)}
    return {
        "story": revised,
        "rewrites": 1,
        "why_ok": state.get("why_ok") if why == story.why_it_matters else None,
        "stats": RunStats(timings_ms=timing),
    }


def drop_reason(story: Story, budget_hit: bool) -> DropReason:
    if budget_hit:
        return DropReason.BUDGET
    verdicts = [c.verdict for c in story.claims]
    if Verdict.UNSUPPORTED in verdicts:
        return DropReason.UNSUPPORTED
    failing = [c for c in story.claims if c.verdict is not Verdict.SUPPORTED]
    if failing and all(c.reasoning == FETCH_FAILED for c in failing):
        return DropReason.FETCH_FAILED
    return DropReason.UNCLEAR_ONLY


async def save_outcome(
    ctx: NewsroomContext, story: Story, status: StoryStatus, reason: DropReason | None
) -> None:
    story_id = uuid.UUID(story.id)
    now = dt.datetime.now(dt.UTC)
    async with ctx.db() as session:
        await session.execute(
            update(db.Story)
            .where(db.Story.id == story_id)
            .values(
                headline=story.headline,
                summary=story.summary,
                why_it_matters=story.why_it_matters,
                status=status,
                drop_reason=reason,
            )
        )
        await session.execute(delete(db.Claim).where(db.Claim.story_id == story_id))
        for claim in story.claims:
            session.add(
                db.Claim(
                    story_id=story_id,
                    text=claim.text,
                    source_url=claim.source_url,
                    verdict=claim.verdict,
                    evidence_url=claim.evidence_url,
                    evidence_quote=claim.evidence_quote,
                    evidence_kind=claim.evidence_kind,
                    reasoning=claim.reasoning,
                    attempts=claim.attempts,
                    checked_at=now if claim.verdict is not None else None,
                )
            )
        await session.commit()


async def finish(state: FactCheckState, runtime: Runtime[NewsroomContext]) -> FactCheckUpdate:
    ctx = runtime.context
    story = state["story"]
    verdicts = [c.verdict for c in story.claims]
    counts = RunStats(
        checked=1,
        claims_supported=verdicts.count(Verdict.SUPPORTED),
        claims_unsupported=verdicts.count(Verdict.UNSUPPORTED),
        claims_unclear=verdicts.count(Verdict.UNCLEAR) + verdicts.count(None),
    )
    if passes(story, state.get("why_ok")) and not state.get("budget_hit"):
        await save_outcome(ctx, story, StoryStatus.VERIFIED, None)
        log.info("verified: %s", story.headline)
        return {"verified": [story], "stats": counts.merge(RunStats(verified=1))}

    reason = drop_reason(story, bool(state.get("budget_hit")))
    await save_outcome(ctx, story, StoryStatus.DROPPED, reason)
    log.info("dropped (%s): %s", reason, story.headline)
    dropped = DroppedStory(
        story_id=story.id,
        headline=story.headline,
        beat=story.beat,
        reason=reason,
        claims=story.claims,
    )
    return {"dropped": [dropped], "stats": counts.merge(RunStats(dropped=1))}


def build_fact_check_graph() -> CompiledStateGraph[
    FactCheckState, NewsroomContext, FactCheckInput, FactCheckOutput
]:
    builder = StateGraph(
        FactCheckState,
        context_schema=NewsroomContext,
        input_schema=FactCheckInput,
        output_schema=FactCheckOutput,
    )
    builder.add_node("check", check)
    builder.add_node("rewrite", rewrite)
    builder.add_node("finish", finish)
    builder.add_edge(START, "check")
    builder.add_conditional_edges("check", after_check, ["rewrite", "finish"])
    builder.add_edge("rewrite", "check")
    builder.add_edge("finish", END)
    return builder.compile(name="fact_check_story")
