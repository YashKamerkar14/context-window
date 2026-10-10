"""The newsroom's sources, watchlist and interests (PLAN.md 8). Admin only.

GET returns them in the settings-file shape; PUT applies a settings file exactly as
`cw settings load` does (learned interest weights are kept).
"""

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from context_window.api.deps import AdminOnly, SessionDep
from context_window.db import models as db
from context_window.settings_file import SettingsFile, sync_settings

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[AdminOnly])


@router.get("")
async def get_settings(session: SessionDep) -> dict[str, Any]:
    sources = (await session.scalars(select(db.Source).where(db.Source.enabled.is_(True)))).all()
    repos = (
        await session.scalars(select(db.WatchlistRepo).where(db.WatchlistRepo.enabled.is_(True)))
    ).all()
    interests = (await session.scalars(select(db.Interest).order_by(db.Interest.id))).all()
    return {
        "sources": [
            {
                "name": s.name,
                "beat": s.beat,
                "kind": s.kind,
                "url": s.url,
                "authority": s.authority,
                "options": s.options,
            }
            for s in sources
        ],
        "watchlist": [
            {
                "owner": r.owner,
                "repo": r.repo,
                "pypi_package": r.pypi_package,
                "tag_pattern": r.tag_pattern,
            }
            for r in repos
        ],
        "interests": [
            {"topic": i.topic, "description": i.description, "weight": i.weight} for i in interests
        ],
    }


@router.put("")
async def put_settings(body: SettingsFile, session: SessionDep) -> dict[str, int]:
    report = await sync_settings(session, body)
    await session.commit()
    return {
        "sources": report.sources,
        "sources_disabled": report.sources_disabled,
        "repos": report.repos,
        "repos_disabled": report.repos_disabled,
        "interests": report.interests,
        "interests_deleted": report.interests_deleted,
    }
