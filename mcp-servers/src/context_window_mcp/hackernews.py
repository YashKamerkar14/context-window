"""Hacker News stories through the Algolia search API.

Run on its own: `python -m context_window_mcp.hackernews` (stdio).
"""

import datetime as dt
import json
from typing import Any, cast

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from context_window_mcp.common import (
    ClientFactory,
    build_server,
    default_client_factory,
    http_client,
    run_stdio,
)
from context_window_mcp.http import FetchError

API = "https://hn.algolia.com/api/v1/search_by_date"


class Story(BaseModel):
    hn_id: int
    title: str
    url: str | None = Field(description="The linked article; None for Ask HN and text posts")
    hn_url: str
    points: int
    comment_count: int
    author: str | None
    created_at: dt.datetime


class StoriesResult(BaseModel):
    stories: list[Story] = Field(default_factory=list[Story])
    total_hits: int = 0
    error: str | None = None


def _story(hit: dict[str, Any]) -> Story | None:
    object_id = str(hit.get("objectID") or "")
    created = hit.get("created_at_i")
    if not object_id.isdigit() or not isinstance(created, int):
        return None
    return Story(
        hn_id=int(object_id),
        title=str(hit.get("title") or "").strip(),
        url=cast(str | None, hit.get("url")) or None,
        hn_url=f"https://news.ycombinator.com/item?id={object_id}",
        points=int(hit.get("points") or 0),
        comment_count=int(hit.get("num_comments") or 0),
        author=cast(str | None, hit.get("author")),
        created_at=dt.datetime.fromtimestamp(created, tz=dt.UTC),
    )


def create_server(client_factory: ClientFactory = default_client_factory) -> FastMCP:
    mcp = build_server(
        "hackernews", "Searches recent Hacker News stories by keyword and points.", client_factory
    )

    @mcp.tool
    async def search_stories(
        query: str,
        since: dt.datetime,
        ctx: Context,
        min_points: int = Field(default=50, ge=0),
        max_results: int = Field(default=50, ge=1, le=200),
    ) -> StoriesResult:
        """Stories matching `query` (empty matches everything) created at or after `since`
        with at least `min_points`, newest first."""
        since_ts = int(since.timestamp())
        params: dict[str, str | int] = {
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>={since_ts},points>={min_points}",
            "hitsPerPage": max_results,
        }
        try:
            fetched = await http_client(ctx).get(API, params=params)
        except FetchError as exc:
            return StoriesResult(error=str(exc))
        if not fetched.ok:
            return StoriesResult(error=f"HTTP {fetched.status}")
        try:
            body = cast(dict[str, Any], json.loads(fetched.content))
        except ValueError as exc:
            return StoriesResult(error=f"unexpected response: {exc}")
        hits = cast(list[dict[str, Any]], body.get("hits", []))
        stories = [s for s in (_story(h) for h in hits) if s is not None]
        return StoriesResult(stories=stories, total_hits=int(body.get("nbHits") or 0))

    return mcp


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
