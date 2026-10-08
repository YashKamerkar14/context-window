import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.db.models import Item, Source
from context_window.db.queries import NewItem, save_items
from context_window.runner import consume_candidates, run_date_for
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
    assert await consume_candidates(session, [story], at=dt.datetime.now(dt.UTC)) == 1
    consumed = await session.scalars(
        select(Item.canonical_key).where(Item.consumed_at.is_not(None))
    )
    assert list(consumed) == ["gh:langchain-ai/langgraph@1.2.14"]
