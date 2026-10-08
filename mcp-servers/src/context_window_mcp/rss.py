"""RSS and Atom feeds (lab blogs, PyPI release feeds).

Run on its own: `python -m context_window_mcp.rss` (stdio).
"""

import datetime as dt

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from context_window_mcp.common import (
    ClientFactory,
    build_server,
    default_client_factory,
    http_client,
    is_since,
    run_stdio,
    strip_html,
    truncate,
)
from context_window_mcp.feeds import parse_feed
from context_window_mcp.http import FetchError

SUMMARY_CHARS = 500


class FeedItem(BaseModel):
    title: str
    link: str | None
    summary: str = Field(description="Plain-text summary, at most 500 characters")
    published_at: dt.datetime | None
    guid: str | None


class FeedResult(BaseModel):
    url: str
    feed_title: str | None = None
    items: list[FeedItem] = Field(default_factory=list[FeedItem])
    error: str | None = None


def create_server(client_factory: ClientFactory = default_client_factory) -> FastMCP:
    mcp = build_server(
        "rss",
        "Reads RSS and Atom feeds. Returns entries newest first with plain-text summaries.",
        client_factory,
    )

    @mcp.tool
    async def fetch_feed(
        url: str,
        ctx: Context,
        since: dt.datetime | None = None,
        max_items: int = Field(default=50, ge=1, le=200),
    ) -> FeedResult:
        """Fetch a feed and return its entries published at or after `since`, newest first."""
        try:
            fetched = await http_client(ctx).get(url)
        except FetchError as exc:
            return FeedResult(url=url, error=str(exc))
        if not fetched.ok:
            return FeedResult(url=url, error=f"HTTP {fetched.status}")

        parsed = parse_feed(fetched.content)
        if not parsed.is_feed:
            return FeedResult(url=url, error=f"not an RSS or Atom feed ({parsed.problem})")

        entries = [e for e in parsed.entries if is_since(e.published_at, since)]
        epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
        entries.sort(key=lambda e: e.published_at or epoch, reverse=True)
        return FeedResult(
            url=url,
            feed_title=parsed.title,
            items=[
                FeedItem(
                    title=e.title,
                    link=e.link,
                    summary=truncate(strip_html(e.summary), SUMMARY_CHARS),
                    published_at=e.published_at,
                    guid=e.guid,
                )
                for e in entries[:max_items]
            ],
        )

    return mcp


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
