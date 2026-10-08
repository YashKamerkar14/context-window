"""The reporter subgraph: `fetch`, then `write`, for one beat assignment.

Its input is one assignment and its output is candidates plus stats; everything in
between (the leads) is private to the subgraph, and the release notes themselves stay in
the run's fetch cache. A reporter never crashes the run: failures become `stats.errors`
and the reporter returns whatever it has.

Only the releases beat exists so far (Phase 4); the other beats come in Phase 5.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import asyncio
import logging
import operator
import time
import uuid
from collections.abc import Sequence
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from context_window.budget import BudgetExceeded
from context_window.fetching import load_feed_texts, save_feed_texts
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.drafts import ReleaseNotes
from context_window.graph.reporter.releases import (
    ReleaseLead,
    fetch_releases,
    group_releases,
    load_sources,
    reportable_leads,
    save_releases,
    to_candidate,
)
from context_window.graph.state import merge_stats
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import BeatAssignment, RunError, RunStats, StoryCandidate

log = logging.getLogger(__name__)

BATCH_SIZE = 10  # releases per model call (PLAN.md 6.3)
PARALLEL_BATCHES = 3


class ReporterInput(TypedDict):
    run_id: str
    assignment: BeatAssignment


class ReporterOutput(TypedDict, total=False):
    candidates: Annotated[list[StoryCandidate], operator.add]
    stats: Annotated[RunStats, merge_stats]


class ReporterUpdate(TypedDict, total=False):
    """What a node returns: any subset of the private and output fields."""

    leads: list[ReleaseLead]
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
    if assignment.beat is not Beat.RELEASES:
        error = RunError(node="reporter.fetch", source=assignment.beat, message="no reporter yet")
        return {"leads": [], "stats": RunStats(errors=[error])}

    run_id = uuid.UUID(state["run_id"])
    now = ctx.now()
    async with ctx.db() as session:
        sources, repos = await load_sources(session)
    releases, errors = await fetch_releases(ctx.tools, sources, repos, now=now)
    async with ctx.db() as session:
        saved = await save_releases(session, releases)
        leads = await reportable_leads(
            session, group_releases(releases), saved, now=now, max_items=assignment.max_items
        )
        notes = {r.url: r.notes for r in releases if r.from_github and r.notes.strip()}
        await save_feed_texts(
            session, run_id, {lead.url: notes[lead.url] for lead in leads if lead.url in notes}
        )
        await session.commit()

    log.info(
        "releases: fetched %d from %d repos, %d new, %d leads to write",
        len(releases),
        len(repos),
        sum(item.inserted for item in saved.values()),
        len(leads),
    )
    return {
        "leads": leads,
        "stats": RunStats(
            items_fetched_by_beat={Beat.RELEASES: len(releases)},
            errors=errors,
            timings_ms={"reporter.fetch": _ms(started)},
        ),
    }


async def write(state: ReporterState, runtime: Runtime[NewsroomContext]) -> ReporterUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    # A release without notes (e.g. only on PyPI) has nothing to report or check against.
    leads = [lead for lead in state.get("leads", []) if lead.has_notes]
    if not leads:
        return {"candidates": [], "stats": RunStats(timings_ms={"reporter.write": _ms(started)})}

    async with ctx.db() as session:
        notes = await load_feed_texts(
            session, uuid.UUID(state["run_id"]), (lead.url for lead in leads)
        )

    limit = asyncio.Semaphore(PARALLEL_BATCHES)
    errors: list[RunError] = []
    batches = [leads[i : i + BATCH_SIZE] for i in range(0, len(leads), BATCH_SIZE)]

    async def write_batch(batch: Sequence[ReleaseLead]) -> list[StoryCandidate]:
        releases = [
            ReleaseNotes(
                id=number,
                repo=lead.repo,
                package=lead.package,
                version=lead.version,
                published=lead.published_at.date() if lead.published_at else None,
                notes=notes.get(lead.url, ""),
            )
            for number, lead in enumerate(batch, start=1)
        ]
        async with limit:
            try:
                drafts = await ctx.write_releases(releases, state["assignment"])
            except BudgetExceeded as exc:
                errors.append(RunError(node="reporter.write", source="budget", message=str(exc)))
                return []
            except Exception as exc:  # a failed batch must not cost the others
                log.exception("releases batch failed")
                message = f"{type(exc).__name__}: {exc}"
                errors.append(RunError(node="reporter.write", message=message))
                return []

        candidates: list[StoryCandidate] = []
        written: set[int] = set()
        for draft in drafts.stories:
            if not 1 <= draft.release_id <= len(batch) or draft.release_id in written:
                errors.append(
                    RunError(
                        node="reporter.write",
                        message=f"draft cites unknown or repeated release {draft.release_id}",
                    )
                )
                continue
            lead = batch[draft.release_id - 1]
            written.add(draft.release_id)
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
            if 1 <= skipped.release_id <= len(batch):
                lead = batch[skipped.release_id - 1]
                log.info("releases: skipped %s %s: %s", lead.repo, lead.version, skipped.reason)
        return candidates

    results = await asyncio.gather(*(write_batch(batch) for batch in batches))
    candidates = [c for batch in results for c in batch]
    return {
        "candidates": candidates,
        "stats": RunStats(
            candidates=len(candidates),
            errors=errors,
            timings_ms={"reporter.write": _ms(started)},
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
