"""Running the newsroom: starting runs, pausing them for review, resuming them, and
recovering runs whose job died (PLAN.md 2 and 6.5).

Every execution of the graph goes through `Newsroom.execute`: it keeps a heartbeat,
charges a Budget, invokes the graph from its Postgres checkpoint (thread id = run id),
and records the outcome on the `runs` row:

- `awaiting_review`: the graph paused at `human_review`; the job can exit.
- `published`: the briefing was published.
- `rejected`: the review rejected it, or there was nothing to publish.
- `failed`: an exception escaped the graph; `cw resume` retries from the checkpoint.
- An interrupted execution (a cancelled job) leaves the run `running`.

`cw run` and `cw resume` first resume any `running` run whose heartbeat stopped more
than two minutes ago (its job died) from its last checkpoint, and `cw run` expires older
runs still waiting for review.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from context_window.budget import Budget, use_budget
from context_window.config import Settings
from context_window.db.models import Run
from context_window.db.queries import create_run, published_run_for, stale_runs, touch_heartbeat
from context_window.db.session import make_engine, make_sessionmaker
from context_window.embeddings import make_embedder
from context_window.factcheck.judge import make_claim_judge
from context_window.factcheck.rewrite import make_story_rewriter
from context_window.graph.briefing import make_draft_checker, make_intro_writer
from context_window.graph.checkpoint import postgres_checkpointer
from context_window.graph.context import Agents, NewsroomContext
from context_window.graph.editor import make_coverage_planner
from context_window.graph.newsroom import build_newsroom_graph
from context_window.graph.rank import make_same_event_judge
from context_window.graph.reporter.writer import make_story_writer
from context_window.ids import uuid7
from context_window.mcp_client import NewsroomTools
from context_window.models import chat_model
from context_window.schemas.enums import Beat, RunStatus, RunTrigger
from context_window.schemas.newsroom import (
    Briefing,
    DroppedStory,
    ReviewDecision,
    RunError,
    RunStats,
    Story,
    StoryCandidate,
)
from context_window.tracing import configure_tracing, flush_traces, trace_url

log = logging.getLogger(__name__)

STALE_AFTER = dt.timedelta(minutes=2)


class AlreadyPublishedError(RuntimeError):
    pass


class NothingToResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    run_date: dt.date
    status: RunStatus
    candidates: list[StoryCandidate]
    stories: list[Story]  # the ranked pool
    verified: list[Story]
    dropped: list[DroppedStory]
    draft: Briefing | None
    review_request: dict[str, Any] | None  # what the reviewer sees while paused
    stats: RunStats
    budget: Budget
    trace_url: str | None


@dataclass(frozen=True)
class Outcome:
    result: RunResult | None
    recovered: list[RunResult] = field(default_factory=list[RunResult])


def run_date_for(settings: Settings, now: dt.datetime | None = None) -> dt.date:
    """The briefing date: today in BRIEFING_TZ."""
    moment = now or dt.datetime.now(dt.UTC)
    return moment.astimezone(ZoneInfo(settings.briefing_tz)).date()


def build_agents(settings: Settings) -> Agents:
    fast, strong = chat_model(settings, "fast"), chat_model(settings, "strong")
    return Agents(
        plan_coverage=make_coverage_planner(strong),
        write_stories=make_story_writer(fast),
        same_event=make_same_event_judge(fast),
        judge_claims=make_claim_judge(fast, run_name="judge_claims"),
        judge_unclear=make_claim_judge(strong, run_name="judge_unclear"),
        rewrite_story=make_story_rewriter(fast),
        write_intro=make_intro_writer(strong),
        check_draft=make_draft_checker(fast),
    )


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


def _prior_model_stats(run: Run) -> RunStats:
    """Model usage recorded by earlier executions of this run (before a pause or crash)."""
    stats = run.stats or {}
    return RunStats(
        model_calls=int(stats.get("model_calls", 0)),
        tokens_in=int(stats.get("tokens_in", 0)),
        tokens_out=int(stats.get("tokens_out", 0)),
        cost_usd=float(stats.get("cost_usd", 0.0)),
    )


@dataclass
class Newsroom:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    graph: CompiledStateGraph[Any, NewsroomContext, Any, Any]
    context: NewsroomContext
    tracing: bool

    async def execute(self, run: Run, graph_input: Any) -> RunResult:
        """Invoke the graph for `run` from its checkpoint and record the outcome."""
        settings = self.settings
        prior = _prior_model_stats(run)
        budget = Budget(
            prices=settings.model_prices,
            max_cost_usd=max(0.0, settings.max_cost_per_run_usd - prior.cost_usd),
            max_calls=max(1, settings.max_model_calls_per_run - prior.model_calls),
        )
        first = graph_input is not None and not isinstance(graph_input, Command)
        # A trace's root shares the run's id on the first execution; later ones need their
        # own (LangSmith ids are unique) and carry the run id as metadata.
        trace_id = run.id if first else uuid7()
        config: RunnableConfig = {
            "configurable": {"thread_id": str(run.id)},
            "run_id": trace_id,
            "run_name": "newsroom" if first else "newsroom (resumed)",
            "tags": [f"run_date:{run.run_date.isoformat()}"],
            "metadata": {"run_id": str(run.id), "run_date": run.run_date.isoformat()},
        }
        await self._set(run.id, status=RunStatus.RUNNING, heartbeat_at=func.now())
        try:
            with use_budget(budget):
                async with heartbeat(self.sessions, run.id, settings.worker_heartbeat_seconds):
                    await self.graph.ainvoke(graph_input, config, context=self.context)
        except Exception as exc:
            error = RunError(node="run", message=f"{type(exc).__name__}: {exc}")
            stats = prior.merge(budget.stats()).merge(RunStats(errors=[error]))
            await self._set(
                run.id,
                status=RunStatus.FAILED,
                finished_at=func.now(),
                stats=stats.model_dump(mode="json"),
            )
            raise
        except BaseException:
            # Interrupted (a cancelled Actions job, Ctrl+C): the run stays `running`, its
            # heartbeat stops, and the next `cw run` or `cw resume` continues it.
            log.warning("run %s interrupted; resume it with `cw resume %s`", run.id, run.id)
            raise
        finally:
            if self.tracing:
                await asyncio.to_thread(flush_traces)

        snapshot = await self.graph.aget_state(config)
        values: dict[str, Any] = dict(snapshot.values)
        paused = bool(snapshot.interrupts)
        stats = (values.get("stats") or RunStats()).merge(prior).merge(budget.stats())
        if paused:
            status = RunStatus.AWAITING_REVIEW
        elif values.get("published"):
            status = RunStatus.PUBLISHED
        else:
            status = RunStatus.REJECTED
        trace = await trace_url(settings, trace_id) if self.tracing else None
        changes: dict[str, Any] = {"status": status, "stats": stats.model_dump(mode="json")}
        if trace:
            changes["trace_url"] = trace
        if paused:
            changes["review_requested_at"] = func.now()
        else:
            changes["finished_at"] = func.now()
        await self._set(run.id, **changes)
        log.info("run %s: %s", run.id, status)
        return RunResult(
            run_id=run.id,
            run_date=run.run_date,
            status=status,
            candidates=values.get("candidates", []),
            stories=values.get("stories", []),
            verified=values.get("verified", []),
            dropped=values.get("dropped", []),
            draft=values.get("draft"),
            review_request=dict(snapshot.interrupts[0].value) if paused else None,
            stats=stats,
            budget=budget,
            trace_url=trace,
        )

    async def _set(self, run_id: uuid.UUID, **values: Any) -> None:
        async with self.sessions() as session:
            await session.execute(update(Run).where(Run.id == run_id).values(**values))
            await session.commit()

    async def load(self, run_id: uuid.UUID) -> Run:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
        if run is None:
            raise NothingToResumeError(f"no run {run_id}")
        return run

    async def recover_stale(self, *, exclude: uuid.UUID | None = None) -> list[RunResult]:
        """Resume runs whose job died, from their last checkpoint."""
        async with self.sessions() as session:
            stale = [
                r for r in await stale_runs(session, older_than=STALE_AFTER) if r.id != exclude
            ]
        results: list[RunResult] = []
        for run in stale:
            log.info("resuming run %s: its heartbeat stopped", run.id)
            results.append(await self.execute(run, None))
        return results

    async def expire_waiting(self, *, before: dt.date) -> int:
        """Older runs still waiting for review will not be published: expire them."""
        async with self.sessions() as session:
            result = await session.execute(
                update(Run)
                .where(Run.status == RunStatus.AWAITING_REVIEW, Run.run_date < before)
                .values(status=RunStatus.EXPIRED, finished_at=func.now())
                .returning(Run.id)
            )
            expired = list(result.scalars())
            await session.commit()
        for run_id in expired:
            log.info("expired run %s: it was still waiting for review", run_id)
        return len(expired)


@asynccontextmanager
async def open_newsroom(settings: Settings) -> AsyncGenerator[Newsroom]:
    url = settings.require_direct_url()
    tracing = configure_tracing(settings)
    engine = make_engine(url)
    sessions = make_sessionmaker(engine)
    try:
        async with NewsroomTools(settings=settings) as tools, postgres_checkpointer(url) as saver:
            context = NewsroomContext(
                settings=settings,
                tools=tools,
                db=sessions,
                embed=make_embedder(settings),
                agents=build_agents(settings),
            )
            graph = build_newsroom_graph().compile(checkpointer=saver)
            yield Newsroom(settings, sessions, graph, context, tracing)
    finally:
        await engine.dispose()


async def run_newsroom(
    settings: Settings,
    beats: Sequence[Beat] = (),
    *,
    trigger: RunTrigger = RunTrigger.MANUAL,
    resend: bool = False,
    force: bool = False,
) -> Outcome:
    """Recover dead runs, then run the newsroom for today."""
    async with open_newsroom(settings) as newsroom:
        recovered = await newsroom.recover_stale()
        run_date = run_date_for(settings)
        await newsroom.expire_waiting(before=run_date)
        async with newsroom.sessions() as session:
            if not force and await published_run_for(session, run_date) is not None:
                raise AlreadyPublishedError(
                    f"the briefing for {run_date} is already published; pass --force to redo it"
                )
            run = await create_run(
                session,
                run_date=run_date,
                trigger=trigger,
                actions_run_url=settings.actions_run_url,
            )
            await session.commit()
        log.info("run %s started for %s", run.id, run_date)
        result = await newsroom.execute(
            run,
            {
                "run_id": str(run.id),
                "run_date": run_date,
                "beats": list(beats),
                "resend": resend,
            },
        )
        return Outcome(result, recovered)


async def record_review(settings: Settings, run_id: uuid.UUID, decision: ReviewDecision) -> None:
    """Save a review decision for a paused run; `resume_run` applies it."""
    engine = make_engine(settings.require_direct_url(), null_pool=True)
    try:
        async with make_sessionmaker(engine)() as session:
            run = await session.get(Run, run_id)
            if run is None or run.status not in (
                RunStatus.AWAITING_REVIEW,
                RunStatus.RESUME_REQUESTED,
            ):
                state = "missing" if run is None else run.status
                raise NothingToResumeError(f"run {run_id} is not waiting for review ({state})")
            run.review_decision = decision.model_dump(mode="json")
            run.status = RunStatus.RESUME_REQUESTED
            await session.commit()
    finally:
        await engine.dispose()


async def resume_run(settings: Settings, run_id: uuid.UUID) -> Outcome:
    """Continue a run: with its review decision if it paused, from its last checkpoint if
    its job died."""
    async with open_newsroom(settings) as newsroom:
        recovered = await newsroom.recover_stale(exclude=run_id)
        run = await newsroom.load(run_id)
        if run.status in (RunStatus.AWAITING_REVIEW, RunStatus.RESUME_REQUESTED):
            if run.review_decision is None:
                raise NothingToResumeError(
                    f"run {run_id} is waiting for review; decide with `cw review` first"
                )
            graph_input: Any = Command(resume=run.review_decision)
        elif run.status in (RunStatus.RUNNING, RunStatus.FAILED):
            graph_input = None  # continue from the last checkpoint
        else:
            raise NothingToResumeError(f"run {run_id} has nothing to resume ({run.status})")
        return Outcome(await newsroom.execute(run, graph_input), recovered)


async def review_timeouts(settings: Settings, now: dt.datetime | None = None) -> list[RunResult]:
    """Approve every run that has waited for review longer than REVIEW_TIMEOUT_HOURS."""
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(hours=settings.review_timeout_hours)
    engine = make_engine(settings.require_direct_url(), null_pool=True)
    try:
        async with make_sessionmaker(engine)() as session:
            waiting = list(
                (
                    await session.scalars(
                        select(Run.id).where(
                            Run.status == RunStatus.AWAITING_REVIEW,
                            Run.review_requested_at < cutoff,
                        )
                    )
                ).all()
            )
    finally:
        await engine.dispose()
    results: list[RunResult] = []
    for run_id in waiting:
        log.info("run %s waited past the review timeout: approving", run_id)
        await record_review(
            settings, run_id, ReviewDecision(action="approve", decided_by="timeout")
        )
        outcome = await resume_run(settings, run_id)
        if outcome.result is not None:
            results.append(outcome.result)
    return results
