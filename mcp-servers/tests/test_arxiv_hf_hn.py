import datetime as dt
from urllib.parse import parse_qsl, urlsplit

import httpx

from context_window_mcp import arxiv, hackernews, hf_papers
from tests.conftest import call, mocked, replay
from tests.replay import load_index

# --- arXiv ----------------------------------------------------------------------------


async def test_search_recent_parses_papers() -> None:
    async with replay(arxiv.create_server, "arxiv_search") as (client, transport):
        result = await call(client, "search_recent", categories=["cs.CL", "cs.AI"], max_results=5)
    assert result["error"] is None
    papers = result["papers"]
    assert [p["arxiv_id"] for p in papers] == [
        "2610.10538",
        "2610.10536",
        "2610.10533",
        "2610.10528",
        "2610.10526",
    ]
    first = papers[0]
    assert first["version"] == 1
    assert first["url"] == "https://arxiv.org/abs/2610.10538"
    assert first["title"] and "\n" not in first["title"]
    assert first["authors"] and first["categories"]
    assert first["primary_category"] in first["categories"]
    params = dict(parse_qsl(urlsplit(transport.requests[0]).query))
    assert params["search_query"] == "cat:cs.CL OR cat:cs.AI"


async def test_get_paper() -> None:
    async with replay(arxiv.create_server, "arxiv_get") as (client, _):
        result = await call(client, "get_paper", arxiv_id="2610.03799")
    paper = result["paper"]
    assert paper["arxiv_id"] == "2610.03799"
    assert paper["primary_category"] == "cs.CV"
    assert paper["published_at"] is not None


async def test_rejects_bad_categories_without_a_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    async with mocked(arxiv.create_server, handler) as client:
        empty = await call(client, "search_recent", categories=[])
        bad = await call(client, "search_recent", categories=["cs.CL) OR (all:x"])
    assert empty["error"] and bad["error"]


async def test_unknown_paper() -> None:
    feed = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>arXiv Query</title></feed>"
    )
    async with mocked(arxiv.create_server, lambda r: httpx.Response(200, text=feed)) as client:
        result = await call(client, "get_paper", arxiv_id="9999.99999")
    assert result["paper"] is None and "no arXiv paper" in result["error"]


# --- Hugging Face daily papers ---------------------------------------------------------


async def test_daily_papers_sorted_by_upvotes() -> None:
    async with replay(hf_papers.create_server, "hf_daily") as (client, _):
        result = await call(client, "list_daily", limit=10)
    papers = result["papers"]
    assert len(papers) == 10
    assert papers[0]["arxiv_id"] == "2610.10437" and papers[0]["upvotes"] == 14
    assert [p["upvotes"] for p in papers] == sorted((p["upvotes"] for p in papers), reverse=True)
    assert papers[0]["url"] == "https://huggingface.co/papers/2610.10437"
    assert papers[0]["arxiv_url"] == "https://arxiv.org/abs/2610.10437"


async def test_daily_papers_for_a_date() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with mocked(hf_papers.create_server, handler) as client:
        result = await call(client, "list_daily", date="2026-10-07")
    assert result["papers"] == []
    assert seen[0].url.params["date"] == "2026-10-07"


# --- Hacker News ----------------------------------------------------------------------


def _recorded_since() -> dt.datetime:
    _, [exchange] = load_index("hn_search")
    filters = dict(parse_qsl(urlsplit(exchange.url).query))["numericFilters"]
    created = filters.split(",")[0].split(">=")[1]
    return dt.datetime.fromtimestamp(int(created), tz=dt.UTC)


async def test_search_stories() -> None:
    since = _recorded_since()
    async with replay(hackernews.create_server, "hn_search") as (client, _):
        result = await call(
            client,
            "search_stories",
            query="AI",
            since=since.isoformat(),
            min_points=50,
            max_results=10,
        )
    stories = result["stories"]
    assert result["total_hits"] == 13
    assert len(stories) == 10
    top = stories[0]
    assert top["hn_id"] == 49997161 and top["points"] == 361
    assert top["hn_url"] == "https://news.ycombinator.com/item?id=49997161"
    assert all(s["points"] >= 50 for s in stories)
    assert all(dt.datetime.fromisoformat(s["created_at"]) >= since for s in stories)


async def test_text_posts_have_no_url() -> None:
    hit = {
        "objectID": "1",
        "title": "Ask HN: x",
        "url": None,
        "points": 60,
        "created_at_i": 1_791_312_600,
    }
    body = {"hits": [hit, {"objectID": "bad"}], "nbHits": 2}
    async with mocked(hackernews.create_server, lambda r: httpx.Response(200, json=body)) as client:
        result = await call(client, "search_stories", query="", since="2026-10-01T00:00:00Z")
    assert [s["url"] for s in result["stories"]] == [None]  # malformed hit skipped
