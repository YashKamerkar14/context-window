"""Hugging Face daily papers: the research beat's primary list.

Run on its own: `python -m context_window_mcp.hf_papers` (stdio).
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
    parse_iso,
    run_stdio,
    truncate,
)
from context_window_mcp.http import FetchError

API = "https://huggingface.co/api/daily_papers"
SUMMARY_CHARS = 2000


class DailyPaper(BaseModel):
    arxiv_id: str
    title: str
    summary: str
    upvotes: int
    num_comments: int
    url: str = Field(description="The Hugging Face papers page")
    arxiv_url: str
    published_at: dt.datetime | None = Field(description="When it was featured on Hugging Face")


class DailyPapersResult(BaseModel):
    date: dt.date | None
    papers: list[DailyPaper] = Field(default_factory=list[DailyPaper])
    error: str | None = None


def _paper(raw: dict[str, Any]) -> DailyPaper | None:
    paper = cast(dict[str, Any], raw.get("paper") or {})
    arxiv_id = str(paper.get("id") or "")
    if not arxiv_id:
        return None
    return DailyPaper(
        arxiv_id=arxiv_id,
        title=" ".join(str(paper.get("title") or raw.get("title") or "").split()),
        summary=truncate(
            " ".join(str(paper.get("summary") or raw.get("summary") or "").split()), SUMMARY_CHARS
        ),
        upvotes=int(paper.get("upvotes") or 0),
        num_comments=int(raw.get("numComments") or 0),
        url=f"https://huggingface.co/papers/{arxiv_id}",
        arxiv_url=f"https://arxiv.org/abs/{arxiv_id}",
        published_at=parse_iso(cast(str | None, raw.get("publishedAt"))),
    )


def create_server(client_factory: ClientFactory = default_client_factory) -> FastMCP:
    mcp = build_server(
        "hf-papers", "Lists the papers featured on Hugging Face daily papers.", client_factory
    )

    @mcp.tool
    async def list_daily(
        ctx: Context,
        date: dt.date | None = None,
        limit: int = Field(default=50, ge=1, le=100),
    ) -> DailyPapersResult:
        """Papers featured on `date` (default: the latest list), most upvoted first."""
        params: dict[str, str | int] = {"limit": limit}
        if date is not None:
            params["date"] = date.isoformat()
        try:
            fetched = await http_client(ctx).get(API, params=params)
        except FetchError as exc:
            return DailyPapersResult(date=date, error=str(exc))
        if not fetched.ok:
            return DailyPapersResult(date=date, error=f"HTTP {fetched.status}")
        try:
            raw = cast(list[dict[str, Any]], json.loads(fetched.content))
        except (ValueError, TypeError) as exc:
            return DailyPapersResult(date=date, error=f"unexpected response: {exc}")
        papers = [p for p in (_paper(r) for r in raw) if p is not None]
        papers.sort(key=lambda p: p.upvotes, reverse=True)
        return DailyPapersResult(date=date, papers=papers)

    return mcp


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
