"""Health: the API is up, the database answers, and how recently a run heartbeat."""

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, text

from context_window import __version__
from context_window.api.deps import SessionDep
from context_window.db import models as db

router = APIRouter()


class LastRun(BaseModel):
    id: str
    run_date: dt.date
    status: str
    heartbeat_age_seconds: float | None


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    last_run: LastRun | None = None


@router.get("/health")
async def health(session: SessionDep) -> HealthResponse:
    try:
        await session.execute(text("SELECT 1"))
        run = await session.scalar(select(db.Run).order_by(db.Run.started_at.desc()).limit(1))
    except Exception:  # an unreachable database is the answer, not an error
        return HealthResponse(status="degraded", version=__version__, database="unreachable")
    last = None
    if run is not None:
        age = None
        if run.heartbeat_at is not None:
            age = round((dt.datetime.now(dt.UTC) - run.heartbeat_at).total_seconds(), 1)
        last = LastRun(
            id=str(run.id), run_date=run.run_date, status=run.status, heartbeat_age_seconds=age
        )
    return HealthResponse(status="ok", version=__version__, database="ok", last_run=last)
