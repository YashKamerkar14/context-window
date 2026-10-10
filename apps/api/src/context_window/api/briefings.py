"""Published briefings: the latest, one date, and the archive (PLAN.md 8). Public.

Only published briefings are served; drafts stay private until a run publishes them.
"""

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from context_window.api.deps import SessionDep
from context_window.db import models as db
from context_window.schemas.enums import BriefingStatus
from context_window.schemas.newsroom import Briefing, RunStats

router = APIRouter(prefix="/briefings", tags=["briefings"])


class BriefingStats(BaseModel):
    """The three numbers the web app shows above a briefing."""

    items_scanned: int
    stories_kept: int
    claims_verified: int


class BriefingOut(BaseModel):
    date: dt.date
    intro: str
    light_day: bool
    stories: list[dict[str, Any]]  # the Story schema, with claims and sources
    stats: BriefingStats
    run_id: str
    emailed: bool
    body_markdown: str
    previous_date: dt.date | None
    next_date: dt.date | None


class BriefingSummary(BaseModel):
    date: dt.date
    intro: str
    stories: int
    light_day: bool


def _stats(run_stats: dict[str, Any] | None, stories: int) -> BriefingStats:
    stats = RunStats.model_validate(run_stats or {})
    return BriefingStats(
        items_scanned=sum(stats.items_fetched_by_beat.values()),
        stories_kept=stories,
        claims_verified=stats.claims_supported,
    )


def _content(row: db.Briefing) -> Briefing | None:
    return Briefing.model_validate(row.content) if row.content else None


async def _neighbour(session: SessionDep, date: dt.date, *, before: bool) -> dt.date | None:
    published = db.Briefing.status == BriefingStatus.PUBLISHED
    if before:
        stmt = select(db.Briefing.date).where(published, db.Briefing.date < date)
        stmt = stmt.order_by(db.Briefing.date.desc())
    else:
        stmt = select(db.Briefing.date).where(published, db.Briefing.date > date)
        stmt = stmt.order_by(db.Briefing.date)
    return await session.scalar(stmt.limit(1))


async def _out(session: SessionDep, row: db.Briefing) -> BriefingOut:
    run = await session.get(db.Run, row.run_id)
    content = _content(row)
    stories = [s.model_dump(mode="json") for s in content.stories] if content else []
    return BriefingOut(
        date=row.date,
        intro=content.intro if content else "",
        light_day=content.light_day if content else False,
        stories=stories,
        stats=_stats(run.stats if run else None, len(stories)),
        run_id=str(row.run_id),
        emailed=row.emailed_at is not None,
        body_markdown=row.body_markdown,
        previous_date=await _neighbour(session, row.date, before=True),
        next_date=await _neighbour(session, row.date, before=False),
    )


@router.get("/latest")
async def latest(session: SessionDep) -> BriefingOut:
    row = await session.scalar(
        select(db.Briefing)
        .where(db.Briefing.status == BriefingStatus.PUBLISHED)
        .order_by(db.Briefing.date.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no briefing has been published yet")
    return await _out(session, row)


@router.get("")
async def archive(
    session: SessionDep, limit: int = Query(default=60, ge=1, le=365)
) -> list[BriefingSummary]:
    rows = (
        await session.scalars(
            select(db.Briefing)
            .where(db.Briefing.status == BriefingStatus.PUBLISHED)
            .order_by(db.Briefing.date.desc())
            .limit(limit)
        )
    ).all()
    summaries: list[BriefingSummary] = []
    for row in rows:
        content = _content(row)
        summaries.append(
            BriefingSummary(
                date=row.date,
                intro=content.intro if content else "",
                stories=len(content.stories) if content else 0,
                light_day=content.light_day if content else False,
            )
        )
    return summaries


@router.get("/{date}")
async def for_date(date: dt.date, session: SessionDep) -> BriefingOut:
    row = await session.scalar(
        select(db.Briefing).where(
            db.Briefing.date == date, db.Briefing.status == BriefingStatus.PUBLISHED
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no briefing published for {date}")
    return await _out(session, row)
