"""arXiv search through the public export API (Atom).

Run on its own: `python -m context_window_mcp.arxiv` (stdio). arXiv asks for at most one
request every three seconds; the shared HTTP client enforces that.
"""

import datetime as dt
import re
from typing import Any, cast

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
from context_window_mcp.feeds import FeedEntry, parse_feed
from context_window_mcp.http import FetchError

API = "https://export.arxiv.org/api/query"
ABSTRACT_CHARS = 2000
_CATEGORY = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")
_ID_IN_URL = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<version>\d+))?$")


class Paper(BaseModel):
    arxiv_id: str = Field(description="arXiv id without the version, e.g. 2610.03799")
    version: int | None
    title: str
    abstract: str
    authors: list[str]
    url: str
    published_at: dt.datetime | None = Field(description="First version submitted")
    updated_at: dt.datetime | None = Field(description="Latest version submitted")
    primary_category: str | None
    categories: list[str]


class PapersResult(BaseModel):
    papers: list[Paper] = Field(default_factory=list[Paper])
    error: str | None = None


class PaperResult(BaseModel):
    paper: Paper | None = None
    error: str | None = None


def _paper(entry: FeedEntry) -> Paper | None:
    raw = entry.raw
    match = _ID_IN_URL.search(entry.guid or entry.link or "")
    if match is None:
        return None
    arxiv_id = match.group("id")
    version = int(match.group("version")) if match.group("version") else None
    authors = [str(a.get("name", "")) for a in cast(list[dict[str, Any]], raw.get("authors", []))]
    tags = [str(t.get("term", "")) for t in cast(list[dict[str, Any]], raw.get("tags", []))]
    primary = cast(dict[str, Any] | None, raw.get("arxiv_primary_category"))
    published = entry.published_at
    updated_struct = raw.get("updated_parsed")
    updated = (
        dt.datetime(*cast(tuple[int, ...], updated_struct)[:6], tzinfo=dt.UTC)
        if updated_struct
        else None
    )
    return Paper(
        arxiv_id=arxiv_id,
        version=version,
        title=" ".join(entry.title.split()),
        abstract=truncate(strip_html(entry.summary), ABSTRACT_CHARS),
        authors=[a for a in authors if a],
        url=f"https://arxiv.org/abs/{arxiv_id}",
        published_at=published,
        updated_at=updated,
        primary_category=str(primary["term"]) if primary and "term" in primary else None,
        categories=[t for t in tags if t],
    )


async def _query(ctx: Context, params: dict[str, str | int]) -> tuple[list[Paper], str | None]:
    try:
        fetched = await http_client(ctx).get(API, params=params)
    except FetchError as exc:
        return [], str(exc)
    if not fetched.ok:
        return [], f"HTTP {fetched.status}"
    parsed = parse_feed(fetched.content)
    if not parsed.is_feed:
        return [], f"unexpected response from arXiv ({parsed.problem})"
    papers = [p for p in (_paper(e) for e in parsed.entries) if p is not None]
    return papers, None


def create_server(client_factory: ClientFactory = default_client_factory) -> FastMCP:
    mcp = build_server(
        "arxiv",
        "Searches recent arXiv papers by category and fetches single papers.",
        client_factory,
    )

    @mcp.tool
    async def search_recent(
        categories: list[str],
        ctx: Context,
        since: dt.datetime | None = None,
        max_results: int = Field(default=100, ge=1, le=500),
    ) -> PapersResult:
        """Newest papers in any of `categories` (e.g. cs.CL) submitted at or after `since`."""
        bad = [c for c in categories if not _CATEGORY.match(c)]
        if not categories or bad:
            return PapersResult(error=f"invalid categories: {bad or 'none given'}")
        papers, error = await _query(
            ctx,
            {
                "search_query": " OR ".join(f"cat:{c}" for c in categories),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "start": 0,
                "max_results": max_results,
            },
        )
        return PapersResult(
            papers=[p for p in papers if is_since(p.published_at, since)], error=error
        )

    @mcp.tool
    async def get_paper(arxiv_id: str, ctx: Context) -> PaperResult:
        """One paper by arXiv id (with or without a version suffix)."""
        papers, error = await _query(ctx, {"id_list": arxiv_id.strip(), "max_results": 1})
        if error is not None:
            return PaperResult(error=error)
        if not papers:
            return PaperResult(error=f"no arXiv paper with id {arxiv_id}")
        return PaperResult(paper=papers[0])

    return mcp


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
