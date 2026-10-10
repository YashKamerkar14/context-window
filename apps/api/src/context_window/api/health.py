"""Health: the API is up, the database answers, and how recently a run heartbeat."""

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, text

from context_window import __version__
from context_window.api.deps import SessionFactoryDep, SettingsDep
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
async def health(settings: SettingsDep, sessions: SessionFactoryDep) -> HealthResponse:
    """Never fails: an unconfigured or unreachable database is reported, not raised."""
    if settings.database_url is None and settings.database_url_direct is None:
        return HealthResponse(status="degraded", version=__version__, database="not configured")
    try:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            run = await session.scalar(select(db.Run).order_by(db.Run.started_at.desc()).limit(1))
    except Exception:
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
