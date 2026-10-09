"""The whole newsroom graph: editor, four reporters in parallel, merge and rank, and the
draft briefing, with canned sources, stub agents and the rolled-back test database."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import json

import httpx
from context_window_mcp import arxiv, github_releases, hackernews, hf_papers, rss, web_fetch
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db import models as db
from context_window.db.queries import create_run
from context_window.graph.newsroom import build_newsroom_graph
from context_window.schemas.enums import Beat, BriefingStatus, RunTrigger, StoryStatus, Verdict
from context_window.settings_file import InterestSpec, RepoSpec, SettingsFile, sync_settings
from tests import test_desks as desks
from tests import test_reporter_releases as releases
from tests.fakes import StubWriter, newsroom_context, transport

RECORDED = desks.RECORDED


def _route(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "api.github.com":
        return releases.github_handler(request)
    if host == "pypi.org":
        return releases.pypi_handler(request)
    if host in ("huggingface.co", "export.arxiv.org"):
        return desks._research_handler(request)  # pyright: ignore[reportPrivateUsage]
    if host == "hn.algolia.com":
        # One AI story, linking to the lab's own post: the same event on two beats.
        hit = desks._hit(7, "AI lab ships a new model", desks.POST_OK)  # pyright: ignore[reportPrivateUsage]
        return httpx.Response(200, content=json.dumps({"hits": [hit], "nbHits": 1}))
    return desks._industry_handler(request)  # pyright: ignore[reportPrivateUsage]


async def _load_everything(session: AsyncSession) -> None:
    source = desks._source  # pyright: ignore[reportPrivateUsage]
    await sync_settings(
        session,
        SettingsFile(
            sources=[
                source("GitHub", Beat.RELEASES, "github_releases", "https://api.github.com/", 5),
                source("PyPI", Beat.RELEASES, "pypi_releases", "https://pypi.org/rss/project/", 4),
                source(
                    "HF", Beat.RESEARCH, "hf_papers", "https://huggingface.co/api/daily_papers", 3
                ),
                source(
                    "arXiv",
                    Beat.RESEARCH,
                    "arxiv",
                    "https://export.arxiv.org/api/query",
                    3,
                    categories=["cs.CL"],
                ),
                source("Lab blog", Beat.INDUSTRY, "rss", desks.FEED, 5),
                source(
                    "HN",
                    Beat.COMMUNITY,
                    "hackernews",
                    "https://hn.algolia.com/api/v1/search_by_date",
                    2,
                    min_points=50,
                    window_hours=24,
                ),
            ],
            watchlist=[
                RepoSpec(
                    owner="langchain-ai",
                    repo="langgraph",
                    pypi_package="langgraph",
                    tag_pattern=r"\d+\.\d+\.\d+",
                )
            ],
            interests=[InterestSpec(topic="Models", description="new AI models and agents")],
        ),
    )


async def test_the_whole_newsroom(session: AsyncSession) -> None:
    await _load_everything(session)
    run = await create_run(session, run_date=RECORDED.date(), trigger=RunTrigger.MANUAL)
    servers = {
        "github_releases": github_releases,
        "rss": rss,
        "hf_papers": hf_papers,
        "arxiv": arxiv,
        "hackernews": hackernews,
        "web_fetch": web_fetch,
    }
    transports = {name: transport(module.create_server, _route) for name, module in servers.items()}
    writer = StubWriter()
    async with newsroom_context(session, transports, writer=writer, now=lambda: RECORDED) as ctx:
        graph = build_newsroom_graph().compile(checkpointer=InMemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": str(run.id)}}
        state = await graph.ainvoke(
            {"run_id": str(run.id), "run_date": run.run_date}, config, context=ctx
        )

    # Four reporters ran, each writing its own beat (research needed two batches of ten).
    assert {beat for beat, _ in writer.calls} == set(Beat)
    assert [a.beat for a in state["assignments"]] == list(Beat)
    stats = state["stats"]
    assert {f"reporter.{b}.fetch" for b in Beat} <= set(stats.timings_ms)
    assert stats.errors == []

    # The HN story and the lab's post are one story, with both as sources.
    [lab] = [s for s in state["stories"] if desks.POST_OK in s.source_urls]
    assert lab.beat is Beat.INDUSTRY and lab.merged_from == 2
    assert "https://news.ycombinator.com/item?id=7" in lab.source_urls
    assert stats.merged >= 1
    # The only community story was merged into the industry one.
    assert {s.beat for s in state["stories"]} == {Beat.RELEASES, Beat.RESEARCH, Beat.INDUSTRY}
    assert all(0 <= s.score <= 1 for s in state["stories"])

    # Every pooled story was fact-checked (the stub judge supports everything) and saved
    # with its verdicts; the rest stay candidates.
    pool = {s.id for s in state["stories"]}
    assert {s.id for s in state["verified"]} == pool and state["dropped"] == []
    assert stats.checked == stats.verified == len(pool)
    saved = (await session.scalars(select(db.Story).where(db.Story.run_id == run.id))).all()
    assert all(s.embedding is not None for s in saved)
    for row in saved:
        expected = StoryStatus.VERIFIED if str(row.id) in pool else StoryStatus.CANDIDATE
        assert row.status is expected
    verdicts = (
        await session.scalars(
            select(db.Claim.verdict)
            .join(db.Story, db.Story.id == db.Claim.story_id)
            .where(db.Story.status == StoryStatus.VERIFIED)
        )
    ).all()
    assert verdicts and set(verdicts) == {Verdict.SUPPORTED}

    briefing = await session.scalar(select(db.Briefing).where(db.Briefing.date == run.run_date))
    assert briefing is not None and briefing.status is BriefingStatus.DRAFT
    draft = state["draft"]
    assert draft.stories and briefing.body_markdown.startswith("# Context Window, 2026-10-08")
    assert draft.stories[0].headline in briefing.body_markdown
