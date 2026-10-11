"""The daily start signal (PLAN.md 2), called by Vercel Cron (apps/api/vercel.json).

GitHub's own schedule fires hours late on this repository, so Vercel's timer starts the
morning run instead: this endpoint asks GitHub Actions for a scheduled run, unless today's
run is already active or published. The `newsroom.yml` schedule stays as a backup; a
scheduled run that finds today published ends quietly.

Vercel sends `Authorization: Bearer <CRON_SECRET>`; without that secret set, the endpoint
refuses every call.
"""

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from context_window.api.deps import SessionDep, SettingsDep
from context_window.api.dispatch import DispatchError, dispatch
from context_window.dates import run_date_for
from context_window.db import models as db
from context_window.schemas.enums import ACTIVE_RUN_STATUSES, RunStatus

router = APIRouter(prefix="/cron", tags=["cron"])


class CronOut(BaseModel):
    status: Literal["dispatched", "skipped"]
    detail: str


@router.get("/daily-run")
async def daily_run(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CronOut:
    expected = settings.cron_secret.get_secret_value() if settings.cron_secret else None
    scheme, _, token = (authorization or "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "cron secret required")

    today = run_date_for(settings)
    runs = (await session.scalars(select(db.Run).where(db.Run.run_date == today))).all()
    if active := next((r for r in runs if r.status in ACTIVE_RUN_STATUSES), None):
        return CronOut(status="skipped", detail=f"the run for {today} is {active.status}")
    if any(r.status == RunStatus.PUBLISHED for r in runs):
        return CronOut(status="skipped", detail=f"the briefing for {today} is already published")
    try:
        await dispatch(settings, {"command": "run", "trigger": "schedule", "review_mode": "auto"})
    except DispatchError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return CronOut(status="dispatched", detail=f"the newsroom run for {today} was requested")
