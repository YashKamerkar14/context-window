"""`write_briefing`: the day's draft briefing (PLAN.md 6.3).

It takes the top MAX_STORIES stories by score, breaking-change releases first, and sets
`light_day` when fewer than 5 make it. The strong model writes only the one-sentence
intro: the stories keep their checked wording, so the writer cannot add facts.

Until fact-checking exists (Phase 6), the stories are the ranked pool rather than the
verified ones. The draft is saved to `briefings` (one per date; a re-run replaces a
draft, never a published briefing).
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from context_window.budget import BudgetExceeded
from context_window.db import models as db
from context_window.graph.context import IntroWriter, NewsroomContext
from context_window.graph.state import NewsroomState, NewsroomUpdate
from context_window.models import with_network_retry
from context_window.schemas.agents import BriefingIntro
from context_window.schemas.enums import Beat, BriefingStatus
from context_window.schemas.newsroom import Briefing, RunError, RunStats, Story

log = logging.getLogger(__name__)

LIGHT_DAY = 5  # fewer verified stories than this, and the briefing says so

INTRO_SYSTEM = (
    "You write the one-sentence intro of Context Window, a daily briefing for AI "
    "engineers. Name the two or three most important stories in plain words. Use only the "
    "headlines given; add no facts. If it is a light day, say so plainly. No greetings, "
    "no exclamation marks."
)


def order_stories(stories: Sequence[Story], limit: int) -> list[Story]:
    """Breaking-change releases first, then by score."""
    ranked = sorted(stories, key=lambda s: (not s.is_breaking_change, -s.score))
    return ranked[:limit]


def default_intro(stories: Sequence[Story], light_day: bool) -> str:
    if not stories:
        return "No stories cleared the bar today."
    count = f"{len(stories)} {'story' if len(stories) == 1 else 'stories'}"
    if light_day:
        return f"A light day: {count} worth your time, led by {stories[0].headline}."
    return f"Today's {count}, led by {stories[0].headline}."


def _source_label(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def render_markdown(briefing: Briefing) -> str:
    lines = [f"# Context Window, {briefing.date.isoformat()}", "", briefing.intro, ""]
    if briefing.light_day:
        lines += ["_A light day: fewer than five stories cleared the bar._", ""]
    for story in briefing.stories:
        flag = " (breaking change)" if story.is_breaking_change else ""
        lines += [f"## {story.headline}{flag}", "", story.summary, ""]
        lines += [f"**Why it matters:** {story.why_it_matters}", ""]
        sources = ", ".join(f"[{_source_label(url)}]({url})" for url in story.source_urls)
        lines += [f"Sources: {sources}", ""]
    return "\n".join(lines).rstrip() + "\n"


def make_intro_writer(model: BaseChatModel) -> IntroWriter:
    structured = with_network_retry(
        model.with_structured_output(
            BriefingIntro, method="json_schema", strict=True, include_raw=True
        )
    )

    async def write(date: dt.date, stories: Sequence[Story], light_day: bool) -> str:
        headlines = "\n".join(f"- [{s.beat}] {s.headline}" for s in stories)
        prompt = (
            f"Date: {date.isoformat()}\nLight day: {'yes' if light_day else 'no'}\n\n{headlines}"
        )
        result = cast(
            dict[str, Any],
            await structured.ainvoke(
                [SystemMessage(INTRO_SYSTEM), HumanMessage(prompt)],
                config={"run_name": "briefing_intro_model"},
            ),
        )
        parsed = result.get("parsed")
        if not isinstance(parsed, BriefingIntro) or not parsed.intro.strip():
            raise RuntimeError(f"the intro did not fit the schema: {result.get('parsing_error')}")
        return " ".join(parsed.intro.split())

    return write


async def save_draft(
    ctx: NewsroomContext, run_id: uuid.UUID, briefing: Briefing, body: str
) -> bool:
    """Upsert the draft for its date. Returns False when that date is already published."""
    async with ctx.db() as session:
        status = await session.scalar(
            select(db.Briefing.status).where(db.Briefing.date == briefing.date)
        )
        if status == BriefingStatus.PUBLISHED:
            return False
        stmt = insert(db.Briefing).values(
            run_id=run_id, date=briefing.date, status=BriefingStatus.DRAFT, body_markdown=body
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[db.Briefing.date],
                set_={"run_id": stmt.excluded.run_id, "body_markdown": stmt.excluded.body_markdown},
            )
        )
        await session.commit()
    return True


async def write_briefing(state: NewsroomState, runtime: Runtime[NewsroomContext]) -> NewsroomUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    run_date = state["run_date"]
    # Phase 6 replaces this with the fact-checked stories (state["verified"]).
    stories = order_stories(state.get("stories", []), ctx.settings.max_stories)
    light_day = len(stories) < LIGHT_DAY
    errors: list[RunError] = []
    intro = default_intro(stories, light_day)
    if stories:
        try:
            intro = await ctx.agents.write_intro(run_date, stories, light_day)
        except BudgetExceeded as exc:
            errors.append(RunError(node="write_briefing", source="budget", message=str(exc)))
        except Exception as exc:  # the default intro will do
            log.exception("intro writer failed")
            errors.append(RunError(node="write_briefing", message=f"{type(exc).__name__}: {exc}"))

    briefing = Briefing(
        date=run_date, intro=intro, stories=stories, light_day=light_day, stats=RunStats()
    )
    saved = await save_draft(ctx, uuid.UUID(state["run_id"]), briefing, render_markdown(briefing))
    if not saved:
        message = f"the briefing for {run_date} is already published; the draft was not saved"
        errors.append(RunError(node="write_briefing", message=message))
    beats = {beat: sum(s.beat is beat for s in stories) for beat in Beat}
    log.info("briefing draft: %d stories %s, light day: %s", len(stories), beats, light_day)
    elapsed = round((time.perf_counter() - started) * 1000)
    return {
        "draft": briefing,
        "stats": RunStats(errors=errors, timings_ms={"write_briefing": elapsed}),
    }
