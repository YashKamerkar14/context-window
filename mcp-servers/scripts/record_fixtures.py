"""Record real responses as test fixtures: `uv run python scripts/record_fixtures.py [name...]`.

Each scenario runs a real tool call through a recording transport. Before saving, bodies
are sanitised so the repository never holds article text (PLAN.md rule 1): feeds keep at
most 10 entries with text cut to 300 characters, and JSON strings are cut to 1,000
characters with lists capped at 10. HTML is recorded only from permissively licensed pages.
"""

import asyncio
import datetime as dt
import json
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client, FastMCP
from lxml import html as lxml_html

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_window_mcp import (
    arxiv,
    github_releases,
    hackernews,
    hf_papers,
    rss,
    web_fetch,
)
from context_window_mcp.http import HttpConfig, PoliteClient
from tests.replay import FIXTURES, KEPT_HEADERS

MAX_ENTRIES = 10
MAX_FEED_TEXT = 300
MAX_JSON_STRING = 1000
# lxml ships without type information.
_lxml_html: Any = lxml_html
_lxml: Any = _lxml_html.etree


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.exchanges: list[tuple[str, int, dict[str, str], bytes, bool]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = await self._inner.handle_async_request(request)
        # aiter_raw gives the bytes as sent (still compressed); aread would decode them.
        encoded = b"".join([chunk async for chunk in raw.aiter_raw()])
        await raw.aclose()
        body = _decode(raw.headers, encoded)
        headers = {k: v for k, v in raw.headers.items() if k.lower() in KEPT_HEADERS}
        was_gzip = raw.headers.get("content-encoding", "").lower() == "gzip"
        headers.pop("content-encoding", None)
        self.exchanges.append((str(request.url), raw.status_code, headers, body, was_gzip))
        return httpx.Response(
            raw.status_code,
            headers={k: v for k, v in raw.headers.items() if k.lower() != "content-encoding"},
            content=body,
            request=request,
        )


def _decode(headers: httpx.Headers, encoded: bytes) -> bytes:
    response = httpx.Response(200, headers=headers, stream=httpx.ByteStream(encoded))
    response.read()
    return response.content


def sanitize_feed(body: bytes) -> bytes:
    root: Any = _lxml.fromstring(body)
    for container in [root, *root.iter("{*}channel")]:
        entries = [c for c in container if _local(c.tag) in ("item", "entry")]
        for extra in entries[MAX_ENTRIES:]:
            container.remove(extra)
    for node in root.iter():
        if isinstance(node.text, str) and len(node.text) > MAX_FEED_TEXT:
            node.text = node.text[:MAX_FEED_TEXT] + "…"
    return bytes(_lxml.tostring(root, xml_declaration=True, encoding="utf-8"))


def _local(tag: object) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def sanitize_json(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_JSON_STRING else value[:MAX_JSON_STRING] + "…"
    if isinstance(value, list):
        return [sanitize_json(v) for v in value[:MAX_ENTRIES]]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def sanitize(content_type: str, body: bytes, keep_html: bool) -> tuple[bytes, str]:
    if "json" in content_type:
        return json.dumps(sanitize_json(json.loads(body)), indent=1).encode(), "json"
    if "xml" in content_type or "rss" in content_type or "atom" in content_type:
        return sanitize_feed(body), "xml"
    if "html" in content_type:
        if not keep_html:
            raise SystemExit(
                "refusing to record HTML from a page not marked as permissively licensed"
            )
        return body, "html"
    return body, "txt"


Call = Callable[[Client[Any]], Awaitable[object]]


async def record(
    name: str, server: Callable[..., FastMCP], call: Call, keep_html: bool = False
) -> None:
    transport = RecordingTransport()

    def factory() -> PoliteClient:
        headers: dict[str, str] = {}
        if server is github_releases.create_server:
            headers = dict(github_releases.github_client_factory()._client.headers)  # pyright: ignore[reportPrivateUsage]
        return PoliteClient(HttpConfig(cache=False), transport=transport, headers=headers)

    async with Client(server(factory)) as client:
        await call(client)

    out = FIXTURES / name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    exchanges: list[dict[str, Any]] = []
    for i, (url, status, headers, body, was_gzip) in enumerate(transport.exchanges):
        clean, ext = sanitize(headers.get("content-type", ""), body, keep_html)
        body_file = f"{i:02d}.{ext}"
        (out / body_file).write_bytes(clean)
        headers.pop("authorization", None)
        exchanges.append(
            {
                "url": url,
                "status": status,
                "headers": headers,
                "body_file": body_file,
                "gzip": was_gzip,
            }
        )
    meta = {"recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    (out / "index.json").write_text(
        json.dumps({"meta": meta, "exchanges": exchanges}, indent=1) + "\n", newline="\n"
    )
    print(f"{name}: {len(exchanges)} exchange(s)")


NOW = dt.datetime.now(dt.UTC)
SCENARIOS: dict[str, Callable[[], Awaitable[None]]] = {
    "rss_github_blog": lambda: record(
        "rss_github_blog",
        rss.create_server,
        lambda c: c.call_tool("fetch_feed", {"url": "https://github.blog/ai-and-ml/feed/"}),
    ),
    "rss_deepmind": lambda: record(
        "rss_deepmind",
        rss.create_server,
        lambda c: c.call_tool("fetch_feed", {"url": "https://deepmind.google/blog/rss.xml"}),
    ),
    "rss_pypi_langgraph": lambda: record(
        "rss_pypi_langgraph",
        rss.create_server,
        lambda c: c.call_tool(
            "fetch_feed", {"url": "https://pypi.org/rss/project/langgraph/releases.xml"}
        ),
    ),
    "github_langgraph": lambda: record(
        "github_langgraph",
        github_releases.create_server,
        lambda c: c.call_tool(
            "list_releases", {"owner": "langchain-ai", "repo": "langgraph", "max_items": 100}
        ),
    ),
    "github_get_release": lambda: record(
        "github_get_release",
        github_releases.create_server,
        lambda c: c.call_tool(
            "get_release", {"owner": "langchain-ai", "repo": "langgraph", "tag": "1.2.14"}
        ),
    ),
    "github_missing_repo": lambda: record(
        "github_missing_repo",
        github_releases.create_server,
        lambda c: c.call_tool(
            "list_releases", {"owner": "langchain-ai", "repo": "no-such-repo-cw"}
        ),
    ),
    "arxiv_search": lambda: record(
        "arxiv_search",
        arxiv.create_server,
        lambda c: c.call_tool(
            "search_recent", {"categories": ["cs.CL", "cs.AI"], "max_results": 5}
        ),
    ),
    "arxiv_get": lambda: record(
        "arxiv_get",
        arxiv.create_server,
        lambda c: c.call_tool("get_paper", {"arxiv_id": "2610.03799"}),
    ),
    "hf_daily": lambda: record(
        "hf_daily",
        hf_papers.create_server,
        lambda c: c.call_tool("list_daily", {"limit": 10}),
    ),
    "hn_search": lambda: record(
        "hn_search",
        hackernews.create_server,
        lambda c: c.call_tool(
            "search_stories",
            {
                "query": "AI",
                "since": (NOW - dt.timedelta(hours=48)).isoformat(),
                "min_points": 50,
                "max_results": 10,
            },
        ),
    ),
    # Python documentation is under the PSF licence, so a full page may be stored.
    "web_python_docs": lambda: record(
        "web_python_docs",
        web_fetch.create_server,
        lambda c: c.call_tool(
            "fetch_page", {"url": "https://docs.python.org/3/library/urllib.robotparser.html"}
        ),
        keep_html=True,
    ),
}


async def main(names: list[str]) -> None:
    for name in names or list(SCENARIOS):
        await SCENARIOS[name]()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
