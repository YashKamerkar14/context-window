"""Running the newsroom graph for one run: the `runs` row, the checkpointer, the MCP
sessions, the budget and the heartbeat around one `ainvoke`.

Phase 4 runs reporters only. Nothing is published yet, so a finished run ends as
`rejected` (the plan's "ended, published nothing"), which frees its date for the next run.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from context_window.budget import Budget, use_budget
from context_window.config import Settings
from context_window.db.models import Item, Run
from context_window.db.queries import create_run, mark_items_consumed, touch_heartbeat
from context_window.db.session import make_engine, make_sessionmaker
from context_window.graph.checkpoint import postgres_checkpointer
from context_window.graph.context import NewsroomContext
from context_window.graph.newsroom import build_newsroom_graph
from context_window.graph.reporter.releases_writer import make_release_writer
from context_window.mcp_client import NewsroomTools
from context_window.models import chat_model
from context_window.schemas.enums import Beat, RunStatus, RunTrigger
from context_window.schemas.newsroom import BeatAssignment, RunError, RunStats, StoryCandidate
from context_window.tracing import configure_tracing, flush_traces, trace_url

log = logging.getLogger(__name__)

# What each reporter looks for until the editor (Phase 5) writes assignments.
DEFAULT_FOCUS: dict[Beat, str] = {
    Beat.RELEASES: (
        "Changes in the watchlist tools that affect engineers who use them: new features, "
        "breaking changes, deprecations and security fixes."
    ),
}


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    run_date: dt.date
    candidates: list[StoryCandidate]
    stats: RunStats
    budget: Budget
    consumed_items: int
    trace_url: str | None


def run_date_for(settings: Settings, now: dt.datetime | None = None) -> dt.date:
    """The briefing date: today in BRIEFING_TZ."""
    moment = now or dt.datetime.now(dt.UTC)
    return moment.astimezone(ZoneInfo(settings.briefing_tz)).date()


def assignments_for(settings: Settings, beats: Iterable[Beat]) -> list[BeatAssignment]:
    return [
        BeatAssignment(
            beat=beat,
            focus=DEFAULT_FOCUS.get(beat, f"Today's most important {beat} news."),
            max_items=settings.max_items_per_beat,
        )
        for beat in beats
    ]


@asynccontextmanager
async def heartbeat(
    sessions: async_sessionmaker[AsyncSession], run_id: uuid.UUID, every_seconds: float
) -> AsyncGenerator[None]:
    """Touch the run's heartbeat until the block exits, so a dead job's run looks stale."""

    async def beat() -> None:
        while True:
            await asyncio.sleep(every_seconds)
            try:
                async with sessions() as session:
                    await touch_heartbeat(session, run_id)
                    await session.commit()
            except Exception:  # a missed beat is not worth failing the run for
                log.warning("heartbeat failed", exc_info=True)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


async def consume_candidates(
    session: AsyncSession, candidates: Sequence[StoryCandidate], *, at: dt.datetime
) -> int:
    """Mark the candidates' items consumed, as publishing will. Returns the rows changed."""
    keys = {key for candidate in candidates for key in candidate.canonical_keys}
    if not keys:
        return 0
    ids = await session.scalars(select(Item.id).where(Item.canonical_key.in_(keys)))
    return await mark_items_consumed(session, ids, at=at)


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: RunStatus,
    stats: RunStats,
    trace: str | None = None,
) -> None:
    async with sessions() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                finished_at=func.now(),
                stats=stats.model_dump(mode="json"),
                trace_url=trace,
            )
        )
        await session.commit()


async def run_newsroom(
    settings: Settings,
    beats: Sequence[Beat],
    *,
    trigger: RunTrigger = RunTrigger.MANUAL,
    consume: bool = False,
) -> RunResult:
    url = settings.require_direct_url()
    tracing = configure_tracing(settings)
    engine = make_engine(url)
    sessions = make_sessionmaker(engine)
    try:
        run_date = run_date_for(settings)
        async with sessions() as session:
            run = await create_run(session, run_date=run_date, trigger=trigger)
            await session.commit()
        log.info("run %s started for %s", run.id, run_date)

        budget = Budget(
            prices=settings.model_prices,
            max_cost_usd=settings.max_cost_per_run_usd,
            max_calls=settings.max_model_calls_per_run,
        )
        try:
            state = await _invoke(settings, sessions, run, beats, budget)
        except BaseException as exc:
            error = RunError(node="run", message=f"{type(exc).__name__}: {exc}")
            await _finish(
                sessions, run.id, RunStatus.FAILED, budget.stats().merge(RunStats(errors=[error]))
            )
            raise
        finally:
            if tracing:
                await asyncio.to_thread(flush_traces)
        trace = await trace_url(settings, run.id) if tracing else None

        candidates: list[StoryCandidate] = state.get("candidates", [])
        stats: RunStats = state.get("stats") or RunStats()
        stats = stats.merge(budget.stats())
        consumed = 0
        if consume:
            async with sessions() as session:
                consumed = await consume_candidates(session, candidates, at=dt.datetime.now(dt.UTC))
                await session.commit()
        await _finish(sessions, run.id, RunStatus.REJECTED, stats, trace)
        return RunResult(run.id, run_date, candidates, stats, budget, consumed, trace)
    finally:
        await engine.dispose()


async def _invoke(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    run: Run,
    beats: Sequence[Beat],
    budget: Budget,
) -> dict[str, Any]:
    writer = make_release_writer(chat_model(settings, "fast"))
    async with (
        NewsroomTools(settings=settings) as tools,
        postgres_checkpointer(settings.require_direct_url()) as saver,
        heartbeat(sessions, run.id, settings.worker_heartbeat_seconds),
    ):
        graph = build_newsroom_graph().compile(checkpointer=saver)
        context = NewsroomContext(
            settings=settings, tools=tools, db=sessions, write_releases=writer
        )
        config: RunnableConfig = {
            "configurable": {"thread_id": str(run.id)},
            # The trace's root run shares the run's id, so the two can be matched.
            "run_id": run.id,
            "run_name": "newsroom",
            "tags": [f"run_date:{run.run_date.isoformat()}", *(f"beat:{b}" for b in beats)],
            "metadata": {"run_id": str(run.id), "run_date": run.run_date.isoformat()},
        }
        with use_budget(budget):
            return await graph.ainvoke(
                {
                    "run_id": str(run.id),
                    "run_date": run.run_date,
                    "assignments": assignments_for(settings, beats),
                },
                config,
                context=context,
            )
