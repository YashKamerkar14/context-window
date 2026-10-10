"""`publish` and `update_memory` (PLAN.md 6.3).

publish: the briefing is saved as published (one per date; a re-run replaces it), then
emailed once. The Resend idempotency key `briefing-<date>` stops a second send of the
same date; a forced re-run does not email again unless the run asked to resend. An
email failure is recorded but does not fail the run: the briefing is already published.
The web app's revalidate endpoint is called when it is configured (Phase 8).

update_memory: the published stories are marked `published` and their items consumed,
so later runs do not report them again.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import logging
import time
import uuid

import httpx
from langgraph.runtime import Runtime
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from context_window.db import models as db
from context_window.db.queries import consume_stories
from context_window.email import email_config, idempotency_key, send_briefing
from context_window.graph.briefing import render_markdown
from context_window.graph.context import NewsroomContext
from context_window.graph.state import NewsroomState, NewsroomUpdate
from context_window.schemas.enums import BriefingStatus, StoryStatus
from context_window.schemas.newsroom import RunError, RunStats

log = logging.getLogger(__name__)


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


async def publish(state: NewsroomState, runtime: Runtime[NewsroomContext]) -> NewsroomUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    draft = state.get("draft")
    run_id = uuid.UUID(state["run_id"])
    if draft is None:
        error = RunError(node="publish", message="no draft to publish")
        return {"published": False, "stats": RunStats(errors=[error])}

    async with ctx.db() as session:
        emailed_at = await session.scalar(
            select(db.Briefing.emailed_at).where(db.Briefing.date == draft.date)
        )
        stmt = insert(db.Briefing).values(
            run_id=run_id,
            date=draft.date,
            status=BriefingStatus.PUBLISHED,
            body_markdown=render_markdown(draft),
            content=draft.model_dump(mode="json"),
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[db.Briefing.date],
                set_={
                    "run_id": stmt.excluded.run_id,
                    "status": stmt.excluded.status,
                    "body_markdown": stmt.excluded.body_markdown,
                    "content": stmt.excluded.content,
                },
            )
        )
        await session.commit()
    log.info("published the briefing for %s (%d stories)", draft.date, len(draft.stories))

    errors: list[RunError] = []
    config = email_config(ctx.settings)
    resend_requested = bool(state.get("resend"))
    if config is None:
        log.info("email not configured (RESEND_API_KEY, RESEND_FROM, BRIEFING_EMAIL_TO); not sent")
    elif emailed_at is not None and not resend_requested:
        log.info("the briefing for %s was emailed at %s; not sending again", draft.date, emailed_at)
    else:
        key = idempotency_key(draft) + (f"-{run_id}" if resend_requested else "")
        try:
            email_id = await send_briefing(config, draft, key)
            async with ctx.db() as session:
                await session.execute(
                    update(db.Briefing)
                    .where(db.Briefing.date == draft.date)
                    .values(emailed_at=dt.datetime.now(dt.UTC))
                )
                await session.commit()
            log.info("emailed the briefing to %s (Resend id %s)", config.to, email_id)
        except Exception as exc:  # the briefing is published; the email can be resent
            log.exception("email failed")
            errors.append(
                RunError(node="publish", source="email", message=f"{type(exc).__name__}: {exc}")
            )

    origin, secret = ctx.settings.web_origin, ctx.settings.revalidate_secret
    if origin and secret:
        headers = {"authorization": f"Bearer {secret.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{origin.rstrip('/')}/api/revalidate",
                    headers=headers,
                    json={"date": draft.date.isoformat()},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            errors.append(RunError(node="publish", source="revalidate", message=str(exc)))

    return {
        "published": True,
        "stats": RunStats(errors=errors, timings_ms={"publish": _ms(started)}),
    }


async def update_memory(state: NewsroomState, runtime: Runtime[NewsroomContext]) -> NewsroomUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    draft = state.get("draft")
    if draft is None or not state.get("published"):
        return {}
    async with ctx.db() as session:
        ids = [uuid.UUID(s.id) for s in draft.stories]
        if ids:
            await session.execute(
                update(db.Story).where(db.Story.id.in_(ids)).values(status=StoryStatus.PUBLISHED)
            )
        consumed = await consume_stories(session, draft.stories, at=dt.datetime.now(dt.UTC))
        await session.commit()
    log.info("memory: %d stories published, %d items consumed", len(draft.stories), consumed)
    return {"stats": RunStats(timings_ms={"update_memory": _ms(started)})}
