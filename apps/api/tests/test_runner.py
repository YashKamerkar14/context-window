import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.db.models import Item, Source
from context_window.db.queries import NewItem, consume_stories, save_items
from context_window.runner import run_date_for
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import StoryCandidate
from context_window.settings_file import SettingsFile, SourceSpec, sync_settings
from tests.test_newsroom_schemas import candidate


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_run_date_is_today_in_the_briefing_time_zone() -> None:
    evening_utc = dt.datetime(2026, 10, 8, 20, 0, tzinfo=dt.UTC)
    assert run_date_for(_settings(briefing_tz="Asia/Kolkata"), evening_utc) == dt.date(2026, 10, 9)
    assert run_date_for(_settings(briefing_tz="UTC"), evening_utc) == dt.date(2026, 10, 8)


async def test_consume_marks_every_item_behind_the_candidates(session: AsyncSession) -> None:
    await sync_settings(
        session,
        SettingsFile(
            sources=[
                SourceSpec(
                    name="GitHub Releases",
                    beat=Beat.RELEASES,
                    kind="github_releases",  # pyright: ignore[reportArgumentType]
                    url="https://api.github.com/",  # pyright: ignore[reportArgumentType]
                    authority=5,
                )
            ]
        ),
    )
    source_id = (await session.scalars(select(Source.id))).one()
    gh = "https://github.com/langchain-ai/langgraph/releases/tag"
    await save_items(
        session,
        source_id,
        [NewItem(url=f"{gh}/1.2.14", title="a"), NewItem(url=f"{gh}/1.2.13", title="b")],
    )
    story = StoryCandidate.model_validate(candidate())  # keys: gh:...@1.2.14
    assert await consume_stories(session, [story], at=dt.datetime.now(dt.UTC)) == 1
    consumed = await session.scalars(
        select(Item.canonical_key).where(Item.consumed_at.is_not(None))
    )
    assert list(consumed) == ["gh:langchain-ai/langgraph@1.2.14"]


class _Graph:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def ainvoke(self, *args: object, **kwargs: object) -> None:
        raise self.error


async def _execute(session: AsyncSession, error: BaseException) -> str:
    from contextlib import asynccontextmanager

    from context_window.db.queries import create_run
    from context_window.runner import Newsroom
    from context_window.schemas.enums import RunTrigger

    run = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)

    @asynccontextmanager
    async def sessions():  # pyright: ignore[reportUnusedFunction]
        yield session

    newsroom = Newsroom(
        _settings(model_prices={}),
        sessions,  # pyright: ignore[reportArgumentType]
        _Graph(error),  # pyright: ignore[reportArgumentType]
        None,  # pyright: ignore[reportArgumentType]
        False,
    )
    with pytest.raises(type(error)):
        await newsroom.execute(run, {"run_id": str(run.id)})
    await session.refresh(run)
    return run.status


async def test_an_interrupted_run_stays_running_so_it_can_be_resumed(session: AsyncSession) -> None:
    assert await _execute(session, KeyboardInterrupt()) == "running"
    # Its model usage so far is kept for the run's total.
    from context_window.db.models import Run

    run = (await session.scalars(select(Run).order_by(Run.started_at.desc()))).first()
    assert run is not None and run.stats.get("model_calls") == 0


async def test_an_error_marks_the_run_failed(session: AsyncSession) -> None:
    assert await _execute(session, RuntimeError("boom")) == "failed"
