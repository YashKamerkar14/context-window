"""Fetching evidence pages through the web-fetch server, cached per run in Postgres.

The cache lives in the database rather than in the server process so a run that crashes
and resumes does not fetch the same evidence again. Rows older than 7 days are purged.
"""

import datetime as dt
import uuid

from context_window_mcp.web_fetch import PageResult
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import FetchCache
from context_window.mcp_client import NewsroomTools

CACHE_RETENTION = dt.timedelta(days=7)


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


async def purge_fetch_cache(
    session: AsyncSession, *, older_than: dt.timedelta = CACHE_RETENTION
) -> int:
    result = await session.execute(
        delete(FetchCache).where(FetchCache.fetched_at < func.now() - older_than)
    )
    return int(getattr(result, "rowcount", 0) or 0)
