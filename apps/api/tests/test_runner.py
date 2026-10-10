import datetime as dt
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.dates import run_date_for
from context_window.db.models import Item, Source
from context_window.db.queries import NewItem, consume_stories, save_items
from context_window.email import EmailConfig
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

    async def astream(self, *args: object, **kwargs: object) -> AsyncGenerator[object]:
        raise self.error
        yield  # an async generator, like the real stream


async def _execute(
    session: AsyncSession, error: BaseException, settings: Settings | None = None
) -> str:
    from contextlib import asynccontextmanager

    from context_window.db.queries import create_run
    from context_window.runner import Newsroom
    from context_window.schemas.enums import RunTrigger

    run = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)

    @asynccontextmanager
    async def sessions():  # pyright: ignore[reportUnusedFunction]
        yield session

    newsroom = Newsroom(
        settings or _settings(model_prices={}),
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


_EMAIL: dict[str, object] = {
    "model_prices": {},
    "resend_api_key": "re_test",
    "resend_from": "Context Window <onboarding@resend.dev>",
    "briefing_email_to": "me@example.com",
    "actions_run_url": "https://github.com/o/r/actions/runs/1",
}


async def test_a_failed_run_emails_an_alert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, object]] = []

    async def send(config: EmailConfig, **alert: object) -> str:
        sent.append({"to": config.to, **alert})
        return "email-id"

    monkeypatch.setattr("context_window.runner.send_failure_alert", send)
    assert await _execute(session, RuntimeError("boom"), _settings(**_EMAIL)) == "failed"
    assert len(sent) == 1
    assert sent[0]["to"] == "me@example.com"
    assert sent[0]["error"] == "RuntimeError: boom"
    assert sent[0]["actions_run_url"] == "https://github.com/o/r/actions/runs/1"


async def test_an_alert_that_cannot_be_sent_does_not_hide_the_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def send(config: EmailConfig, **alert: object) -> str:
        raise ConnectionError("resend is down")

    monkeypatch.setattr("context_window.runner.send_failure_alert", send)
    # The run's own error still propagates (not the alert's), and the run is failed.
    assert await _execute(session, RuntimeError("boom"), _settings(**_EMAIL)) == "failed"


def test_the_alert_names_the_date_the_error_and_where_to_look() -> None:
    import uuid

    from context_window.email import render_failure

    run_id = uuid.uuid4()
    text = render_failure(
        run_date=dt.date(2026, 10, 9),
        run_id=run_id,
        error="RuntimeError: boom",
        actions_run_url="https://github.com/o/r/actions/runs/1",
        newsroom_url="https://example.com/newsroom",
    )
    assert "2026-10-09" in text and "RuntimeError: boom" in text
    assert f"cw resume {run_id}" in text
    assert "https://github.com/o/r/actions/runs/1" in text
    assert "https://example.com/newsroom" in text
