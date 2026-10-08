"""The community desk: AI stories that are popular on Hacker News.

It reads stories from the last `window_hours` with at least `min_points` (source
options), keeps those whose title matches the assignment's keywords, and writes from the
linked article: the article is the primary source, never the HN thread (PLAN.md 6.6).
Ask HN and other text posts have no article and are left out.
"""

import datetime as dt
import re
import uuid
from collections.abc import Iterable

from sqlalchemy import select

from context_window.db.models import Source
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.leads import (
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

MAX_STORIES = 200
# Used when the assignment gives no keywords.
DEFAULT_KEYWORDS = (
    "AI",
    "LLM",
    "LLMs",
    "GPT",
    "Claude",
    "Gemini",
    "Llama",
    "Mistral",
    "OpenAI",
    "Anthropic",
    "DeepMind",
    "Hugging Face",
    "machine learning",
    "neural",
    "transformer",
    "agent",
    "agents",
    "agentic",
    "RAG",
    "embedding",
    "embeddings",
    "inference",
    "fine-tuning",
    "MCP",
    "diffusion",
    "chatbot",
    "copilot",
)


def keyword_pattern(keywords: Iterable[str]) -> re.Pattern[str]:
    """Whole-word, case-insensitive match on any keyword ("AI" does not match "said")."""
    words = sorted({k.strip() for k in keywords if k.strip()}, key=len, reverse=True)
    alternatives = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


async def community_desk(
    ctx: NewsroomContext, assignment: BeatAssignment, run_id: uuid.UUID, now: dt.datetime
) -> DeskResult:
    async with ctx.db() as session:
        source = (
            await session.scalars(
                select(Source).where(
                    Source.beat == Beat.COMMUNITY,
                    Source.kind == SourceKind.HACKERNEWS,
                    Source.enabled.is_(True),
                )
            )
        ).first()
        if source is None:
            return DeskResult(leads=[], texts={}, fetched=0)
        source_id, authority, options = source.id, source.authority, dict(source.options)

    window = dt.timedelta(hours=float(options.get("window_hours", 24)))
    try:
        result = await ctx.tools.call(
            "hackernews",
            "search_stories",
            {
                "query": "",
                "since": (now - window).isoformat(),
                "min_points": int(options.get("min_points", 50)),
                "max_results": MAX_STORIES,
            },
        )
    except ToolCallError as exc:
        error = RunError(node="reporter.fetch", source="hackernews", message=str(exc))
        return DeskResult(leads=[], texts={}, fetched=0, errors=[error])
    errors: list[RunError] = []
    if result.get("error"):
        errors.append(RunError(node="reporter.fetch", source="hackernews", message=result["error"]))

    matches = keyword_pattern(assignment.keywords or DEFAULT_KEYWORDS)
    fetched: list[Fetched] = []
    leads: list[Lead] = []
    seen: set[str] = set()
    for story in result.get("stories", []):
        title = str(story.get("title") or "").strip()
        url = story.get("url")
        if not url or not matches.search(title):
            continue
        try:
            key = canonical_key(str(url))
        except ValueError:
            continue
        if key in seen:
            continue  # the same article posted twice
        seen.add(key)
        item = Fetched(
            source_id=source_id,
            authority=authority,
            url=str(url),
            key=key,
            title=title,
            published_at=parse_datetime(story.get("created_at")),
        )
        fetched.append(item)
        leads.append(
            Lead(
                beat=Beat.COMMUNITY,
                title=title,
                url=item.url,
                source_urls=[item.url, str(story["hn_url"])],
                canonical_keys=[key],
                published_at=item.published_at,
                authority=authority,
                has_text=True,
                labels={
                    "hn_points": str(story.get("points", 0)),
                    "hn_comments": str(story.get("comment_count", 0)),
                },
            )
        )

    async with ctx.db() as session:
        saved = await save_fetched(session, fetched)
        leads = await reportable_leads(
            session, leads, saved, now=now, max_items=assignment.max_items
        )
        await session.commit()

    texts, page_errors = await writer_texts(ctx, run_id, leads, {})
    leads = [lead.model_copy(update={"has_text": lead.url in texts}) for lead in leads]
    return DeskResult(leads=leads, texts=texts, fetched=len(fetched), errors=errors + page_errors)
