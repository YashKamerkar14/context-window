"""Article pages for desks whose feeds carry only a short summary (industry, community).

Each lead's page is fetched once through web-fetch and cached for the run, so the
fact-checker (Phase 6) reuses it instead of fetching again. When the page cannot be read
(blocked by robots.txt, a 404, or no main text), the writer falls back to the summary.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence

from context_window_mcp.web_fetch import PageResult

from context_window.fetching import fetch_page, save_pages
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.leads import WRITER_TEXT_CHARS, Lead
from context_window.mcp_client import ToolCallError
from context_window.schemas.newsroom import RunError

PARALLEL_PAGES = 4


async def writer_texts(
    ctx: NewsroomContext,
    run_id: uuid.UUID,
    leads: Sequence[Lead],
    summaries: Mapping[str, str],
) -> tuple[dict[str, str], list[RunError]]:
    """The text to write from, per lead URL: the page's main text, else the summary."""
    limit = asyncio.Semaphore(PARALLEL_PAGES)
    errors: list[RunError] = []

    async def one(url: str) -> PageResult | None:
        async with limit:
            try:
                return await fetch_page(ctx.tools, url)
            except ToolCallError as exc:
                errors.append(RunError(node="reporter.fetch", source=url, message=str(exc)))
                return None

    results = await asyncio.gather(*(one(lead.url) for lead in leads))
    pages = [page for page in results if page is not None]
    async with ctx.db() as session:
        await save_pages(session, run_id, pages)
        await session.commit()

    by_url = {page.url: page for page in pages}
    texts: dict[str, str] = {}
    for lead in leads:
        page = by_url.get(lead.url)
        if page is not None and page.status == "ok" and page.text.strip():
            texts[lead.url] = page.text[:WRITER_TEXT_CHARS]
        elif summary := summaries.get(lead.url, "").strip():
            texts[lead.url] = summary
    return texts, errors
