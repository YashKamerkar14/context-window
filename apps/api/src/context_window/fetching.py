"""Fetching evidence pages through the web-fetch server, cached per run in Postgres.

The cache lives in the database rather than in the server process so a run that crashes
and resumes does not fetch the same evidence again. Rows older than 7 days are purged.
"""

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping

from context_window_mcp.web_fetch import PageResult
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import FetchCache
from context_window.mcp_client import NewsroomTools

CACHE_RETENTION = dt.timedelta(days=7)
# Feed text (release notes, feed summaries) is cached next to pages under its own key, so
# it never collides with the page at the same URL. It is the fact-checker's fallback
# evidence when the page itself cannot be fetched (PLAN.md 6.6).
FEED_PREFIX = "feed:"
FEED_STATUS = "feed"


async def fetch_page_cached(
    session: AsyncSession, tools: NewsroomTools, run_id: uuid.UUID, url: str
) -> PageResult:
    """The page at `url` for this run: from the cache, or fetched and then cached."""
    cached = await session.scalar(
        select(FetchCache.body).where(FetchCache.run_id == run_id, FetchCache.url == url)
    )
    if cached is not None:
        return PageResult.model_validate_json(cached)

    page = PageResult.model_validate(await tools.call("web_fetch", "fetch_page", {"url": url}))
    await session.execute(
        insert(FetchCache)
        .values(run_id=run_id, url=url, status=page.status, body=page.model_dump_json())
        .on_conflict_do_nothing(index_elements=[FetchCache.run_id, FetchCache.url])
    )
    return page


async def save_feed_texts(
    session: AsyncSession, run_id: uuid.UUID, texts: Mapping[str, str]
) -> None:
    """Store feed text for this run, keyed by the item's URL. Re-saving replaces it."""
    if not texts:
        return
    stmt = insert(FetchCache).values(
        [
            {"run_id": run_id, "url": FEED_PREFIX + url, "status": FEED_STATUS, "body": text}
            for url, text in texts.items()
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FetchCache.run_id, FetchCache.url], set_={"body": stmt.excluded.body}
        )
    )


async def load_feed_texts(
    session: AsyncSession, run_id: uuid.UUID, urls: Iterable[str]
) -> dict[str, str]:
    """Feed text saved for this run, by item URL. Missing URLs are left out."""
    keys = [FEED_PREFIX + url for url in urls]
    if not keys:
        return {}
    rows = await session.execute(
        select(FetchCache.url, FetchCache.body).where(
            FetchCache.run_id == run_id, FetchCache.url.in_(keys)
        )
    )
    return {url.removeprefix(FEED_PREFIX): body or "" for url, body in rows}


async def purge_fetch_cache(
    session: AsyncSession, *, older_than: dt.timedelta = CACHE_RETENTION
) -> int:
    result = await session.execute(
        delete(FetchCache).where(FetchCache.fetched_at < func.now() - older_than)
    )
    return int(getattr(result, "rowcount", 0) or 0)
