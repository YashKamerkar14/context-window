"""The research, industry and community desks against in-memory MCP servers.

Research uses recorded Hugging Face and arXiv responses; the clock is pinned to the day
they were recorded so the 72-hour windows hold whenever the tests run.
"""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import email.utils
import json
import uuid
from pathlib import Path

import httpx
from context_window_mcp import arxiv, hackernews, hf_papers, rss, web_fetch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import FetchCache, Item
from context_window.db.queries import create_run
from context_window.graph.reporter.community import community_desk, keyword_pattern
from context_window.graph.reporter.industry import industry_desk
from context_window.graph.reporter.research import research_desk
from context_window.schemas.enums import Beat, RunTrigger
from context_window.schemas.newsroom import BeatAssignment
from context_window.settings_file import InterestSpec, SettingsFile, SourceSpec, sync_settings
from tests.fakes import newsroom_context, transport

FIXTURES = Path(__file__).resolve().parents[3] / "mcp-servers" / "tests" / "fixtures"
RECORDED = dt.datetime(2026, 10, 8, 20, 0, tzinfo=dt.UTC)
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _source(
    name: str, beat: Beat, kind: str, url: str, authority: int, **options: object
) -> SourceSpec:
    return SourceSpec(
        name=name,
        beat=beat,
        kind=kind,  # pyright: ignore[reportArgumentType]
        url=url,  # pyright: ignore[reportArgumentType]
        authority=authority,
        options=dict(options),
    )


async def _run_id(session: AsyncSession, day: dt.date) -> uuid.UUID:
    return (await create_run(session, run_date=day, trigger=RunTrigger.MANUAL)).id


def _assignment(
    beat: Beat, max_items: int = 40, keywords: list[str] | None = None
) -> BeatAssignment:
    return BeatAssignment(beat=beat, focus="test", max_items=max_items, keywords=keywords or [])


# --- Research --------------------------------------------------------------------------


def _research_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "huggingface.co":
        return httpx.Response(200, content=(FIXTURES / "hf_daily" / "00.json").read_bytes())
    if request.url.host == "export.arxiv.org":
        return httpx.Response(200, content=(FIXTURES / "arxiv_search" / "00.xml").read_bytes())
    return httpx.Response(404)


async def test_research_merges_hf_and_arxiv_and_filters_by_interest(session: AsyncSession) -> None:
    await sync_settings(
        session,
        SettingsFile(
            sources=[
                _source(
                    "HF", Beat.RESEARCH, "hf_papers", "https://huggingface.co/api/daily_papers", 3
                ),
                _source(
                    "arXiv",
                    Beat.RESEARCH,
                    "arxiv",
                    "https://export.arxiv.org/api/query",
                    3,
                    categories=["cs.CL", "cs.AI"],
                ),
            ],
            interests=[
                InterestSpec(topic="Memory", description="memory for embodied agents and objects")
            ],
        ),
    )
    run_id = await _run_id(session, RECORDED.date())
    transports = {
        "hf_papers": transport(hf_papers.create_server, _research_handler),
        "arxiv": transport(arxiv.create_server, _research_handler),
    }
    async with newsroom_context(session, transports, now=lambda: RECORDED) as ctx:
        result = await research_desk(ctx, _assignment(Beat.RESEARCH, max_items=3), run_id, RECORDED)

    assert result.errors == []
    assert result.fetched == 15  # 10 from Hugging Face, 5 from arXiv
    assert len(result.leads) == 3
    for lead in result.leads:
        arxiv_id = lead.labels["arxiv_id"]
        assert lead.url == f"https://arxiv.org/abs/{arxiv_id}"
        assert lead.canonical_keys == [f"arxiv:{arxiv_id}"]
        assert result.texts[lead.url].startswith(lead.title)
    # Only the papers that survived the interest filter (twice the cap) were saved.
    saved = (await session.scalars(select(Item.canonical_key).distinct())).all()
    assert 3 <= len(saved) <= 6


# --- Industry --------------------------------------------------------------------------

FEED = "https://lab.example/feed.xml"
POST_OK = "https://lab.example/blog/new-model"
POST_BLOCKED = "https://lab.example/blog/blocked"


def _industry_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == FEED:
        items = "".join(
            f"<item><title>{title}</title><link>{link}</link><description>{summary}</description>"
            f"<pubDate>{email.utils.format_datetime(NOW - dt.timedelta(hours=2))}</pubDate></item>"
            for title, link, summary in [
                ("A new model", POST_OK, "Short summary of the model."),
                ("Blocked post", POST_BLOCKED, "The feed's own summary."),
            ]
        )
        return httpx.Response(200, text=f'<rss version="2.0"><channel>{items}</channel></rss>')
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /blog/blocked\n")
    if url == POST_OK:
        body = "<html><body><article><h1>A new model</h1>" + "<p>It reads long documents.</p>" * 30
        return httpx.Response(
            200, text=body + "</article></body></html>", headers={"content-type": "text/html"}
        )
    return httpx.Response(404)


async def test_industry_writes_from_the_page_and_falls_back_to_the_summary(
    session: AsyncSession,
) -> None:
    await sync_settings(
        session, SettingsFile(sources=[_source("Lab blog", Beat.INDUSTRY, "rss", FEED, 5)])
    )
    run_id = await _run_id(session, NOW.date())
    transports = {
        "rss": transport(rss.create_server, _industry_handler),
        "web_fetch": transport(web_fetch.create_server, _industry_handler),
    }
    async with newsroom_context(session, transports, now=lambda: NOW) as ctx:
        result = await industry_desk(ctx, _assignment(Beat.INDUSTRY), run_id, NOW)

    assert result.errors == []
    assert {lead.url for lead in result.leads} == {POST_OK, POST_BLOCKED}
    assert all(
        lead.authority == 5 and lead.labels == {"source": "Lab blog"} for lead in result.leads
    )
    assert "It reads long documents." in result.texts[POST_OK]
    assert result.texts[POST_BLOCKED] == "The feed's own summary."  # robots.txt said no
    # Both page fetches are cached for the fact-checker, whatever their outcome.
    cached = (await session.execute(select(FetchCache.url, FetchCache.status))).all()
    assert sorted(cached) == [(POST_BLOCKED, "blocked"), (POST_OK, "ok")]


# --- Community -------------------------------------------------------------------------

ARTICLE = "https://blog.example/agents-in-production"


def _hit(object_id: int, title: str, url: str | None, points: int = 120) -> dict[str, object]:
    return {
        "objectID": str(object_id),
        "title": title,
        "url": url,
        "points": points,
        "num_comments": 40,
        "author": "someone",
        "created_at_i": int((NOW - dt.timedelta(hours=3)).timestamp()),
    }


def _community_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "hn.algolia.com":
        hits = [
            _hit(1, "Running LLM agents in production", ARTICLE),
            _hit(2, "Show HN: LLM agents in production (repost)", ARTICLE + "?utm_source=hn"),
            _hit(3, "Ask HN: Which AI tools do you use?", None),
            _hit(4, "A history of the bicycle", "https://bikes.example/history"),
            _hit(5, "He said what about the GPU market", "https://gpu.example/said"),
        ]
        return httpx.Response(200, content=json.dumps({"hits": hits, "nbHits": len(hits)}))
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if str(request.url) == ARTICLE:
        body = "<html><body><article>" + "<p>Agents need retries and tracing.</p>" * 30
        return httpx.Response(
            200, text=body + "</article></body></html>", headers={"content-type": "text/html"}
        )
    return httpx.Response(404)


def test_keywords_match_whole_words_only() -> None:
    pattern = keyword_pattern(["AI", "LLM", "Hugging Face"])
    assert pattern.search("New AI chips") and pattern.search("llm evals")
    assert pattern.search("Hugging Face raises")
    assert not pattern.search("He said hello") and not pattern.search("LLMOps-ish")


async def test_community_keeps_ai_articles_once_and_writes_from_the_article(
    session: AsyncSession,
) -> None:
    await sync_settings(
        session,
        SettingsFile(
            sources=[
                _source(
                    "HN",
                    Beat.COMMUNITY,
                    "hackernews",
                    "https://hn.algolia.com/api/v1/search_by_date",
                    2,
                    min_points=50,
                    window_hours=24,
                )
            ]
        ),
    )
    run_id = await _run_id(session, NOW.date())
    transports = {
        "hackernews": transport(hackernews.create_server, _community_handler),
        "web_fetch": transport(web_fetch.create_server, _community_handler),
    }
    async with newsroom_context(session, transports, now=lambda: NOW) as ctx:
        result = await community_desk(
            ctx, _assignment(Beat.COMMUNITY, keywords=["LLM", "AI"]), run_id, NOW
        )

    [lead] = result.leads  # the repost is the same article; the rest are off-topic or text posts
    assert lead.url == ARTICLE
    assert lead.source_urls == [ARTICLE, "https://news.ycombinator.com/item?id=1"]
    assert lead.labels == {"hn_points": "120", "hn_comments": "40"}
    assert "Agents need retries" in result.texts[ARTICLE]
