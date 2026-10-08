import datetime as dt

import httpx

from context_window_mcp import rss
from tests.conftest import call, mocked, replay


async def test_parses_a_real_rss_feed() -> None:
    async with replay(rss.create_server, "rss_github_blog") as (client, _):
        result = await call(client, "fetch_feed", url="https://github.blog/ai-and-ml/feed/")
    assert result["error"] is None
    assert result["feed_title"]
    items = result["items"]
    assert len(items) == 10
    assert all(item["title"] and item["link"].startswith("https://github.blog/") for item in items)
    assert all(len(item["summary"]) <= rss.SUMMARY_CHARS for item in items)
    assert "<" not in items[0]["summary"]  # HTML stripped
    dates = [item["published_at"] for item in items]
    assert dates == sorted(dates, reverse=True)


async def test_parses_deepmind_and_pypi_feeds() -> None:
    async with replay(rss.create_server, "rss_deepmind") as (client, _):
        deepmind = await call(client, "fetch_feed", url="https://deepmind.google/blog/rss.xml")
    assert deepmind["error"] is None and len(deepmind["items"]) == 10

    url = "https://pypi.org/rss/project/langgraph/releases.xml"
    async with replay(rss.create_server, "rss_pypi_langgraph") as (client, _):
        pypi = await call(client, "fetch_feed", url=url)
    titles = [item["title"] for item in pypi["items"]]
    assert all(
        title.replace(".", "").isdigit() or "rc" in title or "a" in title for title in titles
    )
    assert pypi["items"][0]["link"].startswith("https://pypi.org/project/langgraph/")


async def test_since_filters_old_entries() -> None:
    async with replay(rss.create_server, "rss_github_blog") as (client, _):
        everything = await call(client, "fetch_feed", url="https://github.blog/ai-and-ml/feed/")
        newest = dt.datetime.fromisoformat(everything["items"][0]["published_at"])
        recent = await call(
            client,
            "fetch_feed",
            url="https://github.blog/ai-and-ml/feed/",
            since=newest.isoformat(),
        )
    assert [item["title"] for item in recent["items"]] == [everything["items"][0]["title"]]


async def test_max_items() -> None:
    async with replay(rss.create_server, "rss_github_blog") as (client, _):
        result = await call(
            client, "fetch_feed", url="https://github.blog/ai-and-ml/feed/", max_items=3
        )
    assert len(result["items"]) == 3


async def test_http_error_is_reported_not_raised() -> None:
    async with mocked(rss.create_server, lambda r: httpx.Response(404)) as client:
        result = await call(client, "fetch_feed", url="https://e.example/feed")
    assert result["items"] == [] and result["error"] == "HTTP 404"


async def test_html_page_is_not_a_feed() -> None:
    page = "<html><body><h1>Blog</h1><p>No feed here.</p></body></html>"
    async with mocked(rss.create_server, lambda r: httpx.Response(200, html=page)) as client:
        result = await call(client, "fetch_feed", url="https://e.example/blog")
    assert result["items"] == []
    assert result["error"].startswith("not an RSS or Atom feed")


async def test_empty_feed_is_fine() -> None:
    feed = '<?xml version="1.0"?><rss version="2.0"><channel><title>Quiet</title></channel></rss>'
    async with mocked(rss.create_server, lambda r: httpx.Response(200, text=feed)) as client:
        result = await call(client, "fetch_feed", url="https://e.example/feed")
    assert result == {
        "url": "https://e.example/feed",
        "feed_title": "Quiet",
        "items": [],
        "error": None,
    }


async def test_network_failure_is_reported() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with mocked(rss.create_server, fail) as client:
        result = await call(client, "fetch_feed", url="https://e.example/feed")
    assert result["items"] == [] and "ConnectError" in result["error"]
