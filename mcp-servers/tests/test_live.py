"""Calls the real APIs. Run with `uv run pytest -m live`; never part of CI."""

import datetime as dt
from typing import Any

import pytest
from fastmcp import Client

from context_window_mcp import arxiv, github_releases, hackernews, hf_papers, rss, web_fetch
from tests.conftest import call

pytestmark = pytest.mark.live

NOW = dt.datetime.now(dt.UTC)


async def _live(server: Any, tool: str, **args: Any) -> dict[str, Any]:
    async with Client(server()) as client:
        return await call(client, tool, **args)


async def test_rss_live() -> None:
    result = await _live(
        rss.create_server, "fetch_feed", url="https://huggingface.co/blog/feed.xml", max_items=5
    )
    assert result["error"] is None and result["items"]


async def test_github_releases_live() -> None:
    result = await _live(
        github_releases.create_server,
        "list_releases",
        owner="langchain-ai",
        repo="langgraph",
        tag_pattern=r"\d+\.\d+\.\d+",
        max_items=3,
    )
    assert result["error"] is None and result["releases"]


async def test_arxiv_live() -> None:
    result = await _live(arxiv.create_server, "search_recent", categories=["cs.CL"], max_results=3)
    assert result["error"] is None and len(result["papers"]) == 3


async def test_hf_papers_live() -> None:
    result = await _live(hf_papers.create_server, "list_daily", limit=5)
    assert result["error"] is None and result["papers"]


async def test_hackernews_live() -> None:
    since = (NOW - dt.timedelta(days=3)).isoformat()
    result = await _live(
        hackernews.create_server, "search_stories", query="AI", since=since, min_points=50
    )
    assert result["error"] is None and result["stories"]


async def test_web_fetch_live() -> None:
    result = await _live(
        web_fetch.create_server, "fetch_page", url="https://docs.python.org/3/library/json.html"
    )
    assert result["status"] == "ok" and "JSON" in result["text"]
