"""The reporter subgraph: `fetch`, then `write`, for one beat assignment.

Its input is one assignment and its output is candidates plus stats; the leads in
between are private to the subgraph, and the text the writer works from stays in the
run's fetch cache. A reporter never crashes the run: failures become `stats.errors` and
the reporter returns whatever it has.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import asyncio
import datetime as dt
import logging
import operator
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from context_window.budget import BudgetExceeded
from context_window.fetching import load_feed_texts, save_feed_texts
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.community import community_desk
from context_window.graph.reporter.drafts import LeadText
from context_window.graph.reporter.industry import industry_desk
from context_window.graph.reporter.leads import DeskResult, Lead, to_candidate
from context_window.graph.reporter.releases import releases_desk
from context_window.graph.reporter.research import research_desk
from context_window.graph.state import merge_stats
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import BeatAssignment, RunError, RunStats, StoryCandidate

log = logging.getLogger(__name__)

BATCH_SIZE = 10  # leads per model call (PLAN.md 6.3)
PARALLEL_BATCHES = 3

Desk = Callable[[NewsroomContext, BeatAssignment, uuid.UUID, dt.datetime], Awaitable[DeskResult]]


async def _releases(
    ctx: NewsroomContext, assignment: BeatAssignment, run_id: uuid.UUID, now: dt.datetime
) -> DeskResult:
    return await releases_desk(ctx, assignment, now)


DESKS: dict[Beat, Desk] = {
    Beat.RELEASES: _releases,
    Beat.RESEARCH: research_desk,
    Beat.INDUSTRY: industry_desk,
    Beat.COMMUNITY: community_desk,
}


class ReporterInput(TypedDict):
    run_id: str
    assignment: BeatAssignment


class ReporterOutput(TypedDict, total=False):
    candidates: Annotated[list[StoryCandidate], operator.add]
    stats: Annotated[RunStats, merge_stats]


class ReporterUpdate(TypedDict, total=False):
    """What a node returns: any subset of the private and output fields."""

    leads: list[Lead]
    candidates: Annotated[list[StoryCandidate], operator.add]
    stats: Annotated[RunStats, merge_stats]


class ReporterState(ReporterInput, ReporterUpdate):
    pass


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


async def fetch(state: ReporterState, runtime: Runtime[NewsroomContext]) -> ReporterUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    assignment = state["assignment"]
    beat = assignment.beat
    run_id = uuid.UUID(state["run_id"])
    try:
        result = await DESKS[beat](ctx, assignment, run_id, ctx.now())
    except Exception as exc:  # one beat's failure must not cost the others
        log.exception("%s desk failed", beat)
        error = RunError(node="reporter.fetch", source=beat, message=f"{type(exc).__name__}: {exc}")
        return {"leads": [], "stats": RunStats(errors=[error])}

    async with ctx.db() as session:
        await save_feed_texts(session, run_id, result.texts)
        await session.commit()
    log.info("%s: fetched %d items, %d leads to write", beat, result.fetched, len(result.leads))
    return {
        "leads": result.leads,
        "stats": RunStats(
            items_fetched_by_beat={beat: result.fetched},
            errors=result.errors,
            timings_ms={f"reporter.{beat}.fetch": _ms(started)},
        ),
    }


async def write(state: ReporterState, runtime: Runtime[NewsroomContext]) -> ReporterUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    assignment = state["assignment"]
    beat = assignment.beat
    timing = f"reporter.{beat}.write"
    # A lead without text (a release only on PyPI, an unreadable article) has nothing to
    # report or check against.
    leads = [lead for lead in state.get("leads", []) if lead.has_text]
    if not leads:
        return {"candidates": [], "stats": RunStats(timings_ms={timing: _ms(started)})}

    async with ctx.db() as session:
        texts = await load_feed_texts(
            session, uuid.UUID(state["run_id"]), (lead.url for lead in leads)
        )

    limit = asyncio.Semaphore(PARALLEL_BATCHES)
    errors: list[RunError] = []
    batches = [leads[i : i + BATCH_SIZE] for i in range(0, len(leads), BATCH_SIZE)]

    async def write_batch(batch: Sequence[Lead]) -> list[StoryCandidate]:
        items = [
            LeadText(
                id=number,
                title=lead.title,
                published=lead.published_at.date() if lead.published_at else None,
                text=texts.get(lead.url, ""),
                labels=lead.labels,
            )
            for number, lead in enumerate(batch, start=1)
        ]
        async with limit:
            try:
                drafts = await ctx.agents.write_stories(beat, items, assignment)
            except BudgetExceeded as exc:
                errors.append(RunError(node="reporter.write", source="budget", message=str(exc)))
                return []
            except Exception as exc:  # a failed batch must not cost the others
                log.exception("%s batch failed", beat)
                message = f"{type(exc).__name__}: {exc}"
                errors.append(RunError(node="reporter.write", source=beat, message=message))
                return []

        candidates: list[StoryCandidate] = []
        written: set[int] = set()
        for draft in drafts.stories:
            if not 1 <= draft.item_id <= len(batch) or draft.item_id in written:
                errors.append(
                    RunError(
                        node="reporter.write",
                        source=beat,
                        message=f"draft cites unknown or repeated item {draft.item_id}",
                    )
                )
                continue
            lead = batch[draft.item_id - 1]
            written.add(draft.item_id)
            try:
                candidates.append(to_candidate(draft, lead))
            except ValidationError as exc:
                problems = "; ".join(e["msg"] for e in exc.errors())
                errors.append(
                    RunError(
                        node="reporter.write",
                        source=lead.url,
                        message=f"invalid candidate dropped: {problems}",
                    )
                )
        for skipped in drafts.skipped:
            if 1 <= skipped.item_id <= len(batch):
                lead = batch[skipped.item_id - 1]
                log.info("%s: skipped %s: %s", beat, lead.title, skipped.reason)
        return candidates

    results = await asyncio.gather(*(write_batch(batch) for batch in batches))
    candidates = [c for batch in results for c in batch]
    return {
        "candidates": candidates,
        "stats": RunStats(
            candidates=len(candidates), errors=errors, timings_ms={timing: _ms(started)}
        ),
    }


def build_reporter_graph() -> CompiledStateGraph[
    ReporterState, NewsroomContext, ReporterInput, ReporterOutput
]:
    builder = StateGraph(
        ReporterState,
        context_schema=NewsroomContext,
        input_schema=ReporterInput,
        output_schema=ReporterOutput,
    )
    builder.add_node("fetch", fetch)
    builder.add_node("write", write)
    builder.add_edge(START, "fetch")
    builder.add_edge("fetch", "write")
    builder.add_edge("write", END)
    return builder.compile(name="reporter")
