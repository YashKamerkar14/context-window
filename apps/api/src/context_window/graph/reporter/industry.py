"""The industry desk: posts on the official blogs of AI labs and tool makers.

The organisation's own post is the primary source (PLAN.md 6.6). Feeds carry only a
short summary, so the desk fetches each post it will write about.
"""

import asyncio
import datetime as dt
import uuid

from sqlalchemy import select

from context_window.db.models import Source
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.leads import (
    LOOKBACK,
    DeskResult,
    Fetched,
    Lead,
    reportable_leads,
    save_fetched,
)
from context_window.graph.reporter.pages import writer_texts
from context_window.graph.reporter.releases import parse_datetime
from context_window.mcp_client import ToolCallError
from context_window.memory.canonical import canonical_key
from context_window.schemas.enums import Beat, SourceKind
from context_window.schemas.newsroom import BeatAssignment, RunError

MAX_PER_FEED = 20
PARALLEL_FEEDS = 4


async def industry_desk(
    ctx: NewsroomContext, assignment: BeatAssignment, run_id: uuid.UUID, now: dt.datetime
) -> DeskResult:
    async with ctx.db() as session:
        sources = (
            await session.scalars(
                select(Source).where(
                    Source.beat == Beat.INDUSTRY,
                    Source.kind == SourceKind.RSS,
                    Source.enabled.is_(True),
                )
            )
        ).all()
        feeds = [(s.id, s.name, s.url, s.authority) for s in sources]

    limit = asyncio.Semaphore(PARALLEL_FEEDS)
    errors: list[RunError] = []

    async def read(source_id: int, name: str, url: str, authority: int) -> list[Fetched]:
        async with limit:
            try:
                result = await ctx.tools.call(
                    "rss",
                    "fetch_feed",
                    {"url": url, "since": (now - LOOKBACK).isoformat(), "max_items": MAX_PER_FEED},
                )
            except ToolCallError as exc:
                errors.append(RunError(node="reporter.fetch", source=name, message=str(exc)))
                return []
        if result.get("error"):
            errors.append(RunError(node="reporter.fetch", source=name, message=result["error"]))
        found: list[Fetched] = []
        for entry in result.get("items", []):
            link = entry.get("link")
            title = str(entry.get("title") or "").strip()
            try:
                key = canonical_key(str(link))
            except ValueError:
                continue  # no usable link
            if not title:
                continue
            found.append(
                Fetched(
                    source_id=source_id,
                    authority=authority,
                    url=str(link),
                    key=key,
                    title=title,
                    published_at=parse_datetime(entry.get("published_at")),
                    excerpt=str(entry.get("summary") or ""),
                )
            )
        return found

    batches = await asyncio.gather(*(read(*feed) for feed in feeds))
    names = {feed[0]: feed[1] for feed in feeds}
    fetched = [item for batch in batches for item in batch]

    # One lead per post; a post syndicated in two feeds keeps its first copy.
    by_key: dict[str, Fetched] = {}
    for item in fetched:
        by_key.setdefault(item.key, item)
    leads = [
        Lead(
            beat=Beat.INDUSTRY,
            title=item.title,
            url=item.url,
            source_urls=[item.url],
            canonical_keys=[item.key],
            published_at=item.published_at,
            authority=item.authority,
            has_text=True,
            labels={"source": names[item.source_id]},
        )
        for item in by_key.values()
    ]
    async with ctx.db() as session:
        saved = await save_fetched(session, fetched)
        leads = await reportable_leads(
            session, leads, saved, now=now, max_items=assignment.max_items
        )
        await session.commit()

    summaries = {item.url: item.excerpt for item in by_key.values()}
    texts, page_errors = await writer_texts(ctx, run_id, leads, summaries)
    leads = [lead.model_copy(update={"has_text": lead.url in texts}) for lead in leads]
    return DeskResult(leads=leads, texts=texts, fetched=len(fetched), errors=errors + page_errors)
