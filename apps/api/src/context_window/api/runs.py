"""Runs: status, live events, starting a run and reviewing one (PLAN.md 8).

Starting and resuming go through GitHub Actions (see dispatch.py), so `POST /runs` and
`POST /runs/{id}/review` answer 202: the job has been asked for, not finished. A run
for today that is already active, or already published, is not started again.
"""

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from context_window.api.deps import AdminOnly, IsAdmin, SessionDep, SettingsDep
from context_window.api.dispatch import DispatchError, dispatch
from context_window.dates import run_date_for
from context_window.db import models as db
from context_window.schemas.enums import ACTIVE_RUN_STATUSES, RunStatus
from context_window.schemas.newsroom import ReviewDecision, Story

router = APIRouter(prefix="/runs", tags=["runs"])

# Run stats anyone may see; the rest (model usage and cost, error text) is admin only.
PUBLIC_STATS = (
    "items_fetched_by_beat",
    "candidates",
    "merged",
    "checked",
    "verified",
    "dropped",
    "claims_supported",
    "claims_unsupported",
    "claims_unclear",
    "timings_ms",
)


class RunOut(BaseModel):
    id: str
    run_date: dt.date
    trigger: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    heartbeat_at: dt.datetime | None
    review_requested_at: dt.datetime | None
    stats: dict[str, Any]
    trace_url: str | None = None  # admin only
    actions_run_url: str | None = None  # admin only


class EventOut(BaseModel):
    id: int
    ts: dt.datetime
    node: str
    kind: str
    public: bool
    payload: dict[str, Any]


class StartRun(BaseModel):
    force: bool = False
    resend: bool = False
    # manual: the run pauses for POST /runs/{id}/review before publishing.
    review: Literal["auto", "manual"] = "auto"


class Accepted(BaseModel):
    status: Literal["dispatched"] = "dispatched"
    detail: str


class ReviewIn(BaseModel):
    action: Literal["approve", "edit", "reject"]
    removed_story_ids: list[str] = Field(default_factory=list[str])
    edited_stories: list[Story] = Field(default_factory=list[Story])


def _out(run: db.Run, *, admin: bool) -> RunOut:
    stats = run.stats or {}
    return RunOut(
        id=str(run.id),
        run_date=run.run_date,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
        review_requested_at=run.review_requested_at,
        stats=stats if admin else {k: stats[k] for k in PUBLIC_STATS if k in stats},
        trace_url=run.trace_url if admin else None,
        actions_run_url=run.actions_run_url if admin else None,
    )


async def _run(session: SessionDep, run_id: uuid.UUID) -> db.Run:
    run = await session.get(db.Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
    return run


@router.get("")
async def recent(
    session: SessionDep, is_admin: IsAdmin, limit: int = Query(default=10, ge=1, le=100)
) -> list[RunOut]:
    rows = (
        await session.scalars(select(db.Run).order_by(db.Run.started_at.desc()).limit(limit))
    ).all()
    return [_out(run, admin=is_admin) for run in rows]


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, session: SessionDep, is_admin: IsAdmin) -> RunOut:
    return _out(await _run(session, run_id), admin=is_admin)


@router.get("/{run_id}/events")
async def events(
    run_id: uuid.UUID,
    session: SessionDep,
    is_admin: IsAdmin,
    after: int = Query(default=0, ge=0, description="Only events with a larger id"),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[EventOut]:
    """Events newer than `after`, oldest first. Poll with the last id you saw."""
    await _run(session, run_id)
    stmt = select(db.RunEvent).where(db.RunEvent.run_id == run_id, db.RunEvent.id > after)
    if not is_admin:
        stmt = stmt.where(db.RunEvent.public.is_(True))
    rows = (await session.scalars(stmt.order_by(db.RunEvent.id).limit(limit))).all()
    return [
        EventOut(id=e.id, ts=e.ts, node=e.node, kind=e.kind, public=e.public, payload=e.payload)
        for e in rows
    ]


@router.post("", status_code=status.HTTP_202_ACCEPTED, dependencies=[AdminOnly])
async def start_run(body: StartRun, session: SessionDep, settings: SettingsDep) -> Accepted:
    """Ask GitHub Actions to run today's newsroom, unless a run for today is active or
    (without `force`) already published."""
    today = run_date_for(settings)
    runs = (await session.scalars(select(db.Run).where(db.Run.run_date == today))).all()
    if active := next((r for r in runs if r.status in ACTIVE_RUN_STATUSES), None):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": f"a run for {today} is already {active.status}", "run_id": str(active.id)},
        )
    published = next((r for r in runs if r.status == RunStatus.PUBLISHED), None)
    if published is not None and not body.force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "detail": f"the briefing for {today} is already published; send force=true",
                "run_id": str(published.id),
            },
        )
    try:
        await dispatch(
            settings,
            {
                "command": "run",
                "force": _flag(body.force),
                "resend": _flag(body.resend),
                "review_mode": body.review,
            },
        )
    except DispatchError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return Accepted(detail=f"the newsroom run for {today} was requested; see GET /runs")


@router.post("/{run_id}/review", status_code=status.HTTP_202_ACCEPTED, dependencies=[AdminOnly])
async def review(
    run_id: uuid.UUID, body: ReviewIn, session: SessionDep, settings: SettingsDep
) -> Accepted:
    """Record the review decision, then ask GitHub Actions to resume the run with it."""
    run = await _run(session, run_id)
    if run.status not in (RunStatus.AWAITING_REVIEW, RunStatus.RESUME_REQUESTED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run {run_id} is {run.status}, not waiting")
    decision = ReviewDecision(
        action=body.action,
        removed_story_ids=body.removed_story_ids,
        edited_stories=body.edited_stories,
        decided_by="human",
    )
    run.review_decision = decision.model_dump(mode="json")
    run.status = RunStatus.RESUME_REQUESTED
    await session.commit()
    try:
        await dispatch(settings, {"command": "resume", "run_id": str(run_id)})
    except DispatchError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return Accepted(detail=f"decision recorded ({decision.action}); the run will resume")


def _flag(value: bool) -> str:
    """workflow_dispatch inputs are sent as strings."""
    return "true" if value else "false"
