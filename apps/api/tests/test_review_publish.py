"""Review, publishing, memory and crash recovery, on the releases beat with canned sources,
stub agents, the Postgres checkpointer and the rolled-back test database."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from context_window_mcp import github_releases, rss, web_fetch
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db import models as db
from context_window.db.queries import create_run
from context_window.graph.checkpoint import postgres_checkpointer
from context_window.graph.context import NewsroomContext
from context_window.graph.newsroom import build_newsroom_graph
from context_window.schemas.enums import BriefingStatus, DropReason, RunTrigger, StoryStatus
from tests import test_reporter_releases as releases
from tests.fakes import StubWriter, newsroom_context, settings, transport

NOW = releases.NOW


def _route(request: Any) -> Any:
    if request.url.host == "api.github.com":
        return releases.github_handler(request)
    if request.url.host == "pypi.org":
        return releases.pypi_handler(request)
    import httpx

    return httpx.Response(404)


@asynccontextmanager
async def newsroom(
    session: AsyncSession, checkpoint_url: str, **settings_overrides: Any
) -> AsyncGenerator[
    tuple[CompiledStateGraph[Any, NewsroomContext, Any, Any], NewsroomContext, db.Run]
]:
    await releases.load_release_settings(session)
    run = await create_run(session, run_date=NOW.date(), trigger=RunTrigger.MANUAL)
    await session.flush()
    transports = {
        "github_releases": transport(github_releases.create_server, _route),
        "rss": transport(rss.create_server, _route),
        "web_fetch": transport(web_fetch.create_server, _route),
    }
    async with (
        newsroom_context(session, transports, writer=StubWriter(), now=lambda: NOW) as ctx,
        postgres_checkpointer(checkpoint_url, max_size=2) as saver,
    ):
        context = replace(ctx, settings=settings(**settings_overrides))
        yield build_newsroom_graph().compile(checkpointer=saver), context, run


def _config(run: db.Run) -> RunnableConfig:
    return {"configurable": {"thread_id": str(run.id)}}


def _input(run: db.Run) -> dict[str, Any]:
    return {"run_id": str(run.id), "run_date": run.run_date}


async def test_manual_review_pauses_then_publishes_on_approval(
    session: AsyncSession, checkpoint_tables: str
) -> None:
    async with newsroom(session, checkpoint_tables) as (graph, ctx, run):
        await graph.ainvoke(_input(run), _config(run), context=ctx)
        paused = await graph.aget_state(_config(run))
        [request] = paused.interrupts
        assert [s["headline"] for s in request.value["stories"]] == [
            "langgraph 1.2.14",
            "langgraph 1.2.13",
        ]
        assert paused.values.get("published") is None

        approve = {"action": "approve", "decided_by": "human"}
        state = await graph.ainvoke(Command(resume=approve), _config(run), context=ctx)

    assert state["published"] is True and state["review"].action == "approve"
    briefing = await session.scalar(select(db.Briefing).where(db.Briefing.date == run.run_date))
    assert briefing is not None and briefing.status is BriefingStatus.PUBLISHED
    assert briefing.emailed_at is None  # no Resend key in tests: not sent
    statuses = (
        await session.scalars(select(db.Story.status).where(db.Story.run_id == run.id))
    ).all()
    assert set(statuses) == {StoryStatus.PUBLISHED}
    consumed = (
        await session.scalars(select(db.Item.canonical_key).where(db.Item.consumed_at.is_not(None)))
    ).all()
    assert "gh:langchain-ai/langgraph@1.2.14" in consumed and "pypi:langgraph@1.2.14" in consumed


async def test_the_reviewer_can_remove_a_story(
    session: AsyncSession, checkpoint_tables: str
) -> None:
    async with newsroom(session, checkpoint_tables) as (graph, ctx, run):
        await graph.ainvoke(_input(run), _config(run), context=ctx)
        request = (await graph.aget_state(_config(run))).interrupts[0].value
        removed = request["stories"][1]["id"]
        edit = {"action": "edit", "removed_story_ids": [removed], "decided_by": "human"}
        state = await graph.ainvoke(Command(resume=edit), _config(run), context=ctx)

    assert [s.headline for s in state["draft"].stories] == ["langgraph 1.2.14"]
    assert state["draft"].intro.startswith("A light day: 1 story")
    row = await session.get(db.Story, uuid.UUID(removed))
    assert row is not None
    await session.refresh(row)
    assert row.status is StoryStatus.DROPPED and row.drop_reason is DropReason.REMOVED_BY_EDITOR


async def test_a_rejected_draft_publishes_nothing(
    session: AsyncSession, checkpoint_tables: str
) -> None:
    async with newsroom(session, checkpoint_tables) as (graph, ctx, run):
        await graph.ainvoke(_input(run), _config(run), context=ctx)
        reject = {"action": "reject", "decided_by": "human"}
        state = await graph.ainvoke(Command(resume=reject), _config(run), context=ctx)
    assert not state.get("published")
    briefing = await session.scalar(
        select(db.Briefing.status).where(db.Briefing.date == run.run_date)
    )
    assert briefing is BriefingStatus.DRAFT
    consumed = (
        await session.scalars(select(db.Item.id).where(db.Item.consumed_at.is_not(None)))
    ).all()
    assert consumed == []


async def test_auto_review_publishes_without_pausing(
    session: AsyncSession, checkpoint_tables: str
) -> None:
    async with newsroom(session, checkpoint_tables, review_mode="auto") as (
        graph,
        ctx,
        run,
    ):
        state = await graph.ainvoke(_input(run), _config(run), context=ctx)
        assert (await graph.aget_state(_config(run))).interrupts == ()
    assert state["published"] is True and state["review"].decided_by == "auto"


async def test_a_run_killed_midway_resumes_from_its_checkpoint(
    session: AsyncSession, checkpoint_tables: str
) -> None:
    """The job dies while writing the briefing; resuming does not report or check again."""

    class Killed(BaseException):
        pass

    from tests.fakes import all_supported, plain_intro

    calls = {"judge": 0, "intro": 0}

    async def judge(source: str, claims: Any) -> Any:
        calls["judge"] += 1
        return await all_supported(source, claims)

    async def dies_once(date: Any, stories: Any, light_day: bool) -> str:
        calls["intro"] += 1
        if calls["intro"] == 1:
            raise Killed  # like the Actions job being cancelled
        return await plain_intro(date, stories, light_day)

    async with newsroom(session, checkpoint_tables, review_mode="auto") as (graph, ctx, run):
        agents = replace(ctx.agents, judge_claims=judge, write_intro=dies_once)
        ctx = replace(ctx, agents=agents)
        with pytest.raises(Killed):
            await graph.ainvoke(_input(run), _config(run), context=ctx)
        writer = ctx.agents.write_stories
        assert isinstance(writer, StubWriter) and len(writer.calls) == 1
        judged = calls["judge"]
        # A new job: `ainvoke(None)` continues from the last checkpoint.
        state = await graph.ainvoke(None, _config(run), context=ctx)

    assert state["published"] is True and len(state["verified"]) == 2
    assert len(writer.calls) == 1 and calls["judge"] == judged  # nothing done twice
    assert calls["intro"] == 2


async def test_old_runs_waiting_for_review_expire(session: AsyncSession) -> None:
    yesterday = await create_run(session, run_date=dt.date(2026, 10, 8), trigger=RunTrigger.MANUAL)
    yesterday.status = "awaiting_review"  # pyright: ignore[reportAttributeAccessIssue]
    await session.flush()

    from context_window.runner import Newsroom

    @asynccontextmanager
    async def db_session() -> AsyncGenerator[AsyncSession]:
        yield session

    newsroom_ = Newsroom(settings(), db_session, None, None, False)  # pyright: ignore[reportArgumentType]
    assert await newsroom_.expire_waiting(before=dt.date(2026, 10, 9)) == 1
    await session.refresh(yesterday)
    assert yesterday.status == "expired"
