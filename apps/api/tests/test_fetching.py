import datetime as dt

import httpx
from context_window_mcp import web_fetch
from context_window_mcp.http import HttpConfig, PoliteClient
from fastmcp.client.transports import FastMCPTransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.db.models import FetchCache
from context_window.db.queries import create_run
from context_window.fetching import fetch_page_cached, purge_fetch_cache
from context_window.mcp_client import NewsroomTools
from context_window.schemas.enums import RunTrigger

PAGE = (
    "<html><head><title>Launch</title></head><body><article><p>The model has a context "
    "window of one million tokens and is available today to every developer.</p>"
    "</article></body></html>"
)


def _tools(requests: list[str]) -> NewsroomTools:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=PAGE)

    def factory() -> PoliteClient:
        return PoliteClient(HttpConfig(cache=False), transport=httpx.MockTransport(handler))

    transport = FastMCPTransport(web_fetch.create_server(factory))
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    return NewsroomTools(settings=settings, transports={"web_fetch": transport})


async def test_a_page_is_fetched_once_per_run(session: AsyncSession) -> None:
    run = await create_run(session, run_date=dt.date(2026, 10, 8), trigger=RunTrigger.MANUAL)
    other = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)
    requests: list[str] = []
    async with _tools(requests) as tools:
        first = await fetch_page_cached(session, tools, run.id, "https://lab.example/post")
        again = await fetch_page_cached(session, tools, run.id, "https://lab.example/post")
        assert requests.count("/post") == 1  # second call came from the cache
        await fetch_page_cached(session, tools, other.id, "https://lab.example/post")
        assert requests.count("/post") == 2  # the cache is per run

    assert first.status == "ok" and "one million tokens" in first.text
    assert again == first
    row = await session.scalar(select(FetchCache).where(FetchCache.run_id == run.id))
    assert row is not None and row.status == "ok"


async def test_purge_removes_old_rows(session: AsyncSession) -> None:
    run = await create_run(session, run_date=dt.date(2026, 10, 8), trigger=RunTrigger.MANUAL)
    requests: list[str] = []
    async with _tools(requests) as tools:
        await fetch_page_cached(session, tools, run.id, "https://lab.example/old")
        await fetch_page_cached(session, tools, run.id, "https://lab.example/new")
    await session.execute(
        update(FetchCache)
        .where(FetchCache.url == "https://lab.example/old")
        .values(fetched_at=FetchCache.fetched_at - dt.timedelta(days=8))
    )
    assert await purge_fetch_cache(session) == 1
    remaining = (await session.scalars(select(FetchCache.url))).all()
    assert remaining == ["https://lab.example/new"]
