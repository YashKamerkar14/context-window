"""The HTTP API against the rolled-back test database, with GitHub dispatch faked."""

import datetime as dt
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window import __version__
from context_window.api import runs as runs_api
from context_window.api.app import create_app
from context_window.api.deps import session_dep, session_factory_dep, settings_dep
from context_window.db import models as db
from context_window.db.queries import create_run
from context_window.events import EventLog, public_summary
from context_window.schemas.enums import BriefingStatus, RunStatus, RunTrigger
from context_window.schemas.newsroom import Briefing, RunStats, Story
from tests.fakes import embed_text, settings
from tests.test_newsroom_schemas import candidate

TOKEN = "test-admin-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "https://web.example"


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []

    async def fake_dispatch(_settings: Any, inputs: dict[str, str], **_: Any) -> None:
        calls.append(inputs)

    monkeypatch.setattr(runs_api, "dispatch", fake_dispatch)
    return calls


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient]:
    config = settings(
        admin_token=TOKEN,
        web_origin=ORIGIN,
        briefing_tz="UTC",
        database_url="postgresql://test@localhost/test",  # the session is overridden
    )
    import context_window.api.app as app_module

    original = app_module.get_settings
    app_module.get_settings = lambda: config  # CORS is configured when the app is built
    try:
        app = create_app()
    finally:
        app_module.get_settings = original

    async def test_session() -> AsyncGenerator[AsyncSession]:
        yield session

    app.dependency_overrides[session_dep] = test_session
    app.dependency_overrides[session_factory_dep] = lambda: asynccontextmanager(test_session)
    app.dependency_overrides[settings_dep] = lambda: config
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        yield http


async def _published(session: AsyncSession, date: dt.date, *, status: BriefingStatus) -> db.Run:
    run = await create_run(session, run_date=date, trigger=RunTrigger.SCHEDULE)
    run.status = RunStatus.PUBLISHED if status is BriefingStatus.PUBLISHED else RunStatus.REJECTED
    run.stats = RunStats(
        items_fetched_by_beat={"releases": 30, "research": 70},  # pyright: ignore[reportArgumentType]
        claims_supported=12,
        cost_usd=0.05,
    ).model_dump(mode="json")
    story = Story(**candidate(), id=str(uuid.uuid4()), score=0.6)
    briefing = Briefing(
        date=date, intro="One story.", stories=[story], light_day=True, stats=RunStats()
    )
    session.add(
        db.Briefing(
            run_id=run.id,
            date=date,
            status=status,
            body_markdown="# Context Window",
            content=briefing.model_dump(mode="json"),
        )
    )
    await session.flush()
    return run


# --- Health and briefings --------------------------------------------------------------


async def test_health_reports_the_database_and_the_last_run(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)
    body = (await client.get("/health")).json()
    assert body["status"] == "ok" and body["version"] == __version__ and body["database"] == "ok"
    assert body["last_run"]["status"] == "running"
    assert body["last_run"]["heartbeat_age_seconds"] >= 0


async def test_health_reports_a_missing_database_instead_of_failing(session: AsyncSession) -> None:
    from context_window.api.health import health

    config = settings(database_url=None, database_url_direct=None)
    body = await health(config, lambda: None)  # pyright: ignore[reportArgumentType]
    assert body.status == "degraded" and body.database == "not configured"


async def test_briefings_serve_only_published_days(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    assert (await client.get("/briefings/latest")).status_code == 404
    await _published(session, dt.date(2026, 10, 8), status=BriefingStatus.PUBLISHED)
    await _published(session, dt.date(2026, 10, 9), status=BriefingStatus.PUBLISHED)
    await _published(session, dt.date(2026, 10, 10), status=BriefingStatus.DRAFT)

    latest = (await client.get("/briefings/latest")).json()
    assert latest["date"] == "2026-10-09"
    assert latest["stats"] == {"items_scanned": 100, "stories_kept": 1, "claims_verified": 12}
    assert latest["stories"][0]["claims"][0]["id"] == "c1"
    assert latest["previous_date"] == "2026-10-08" and latest["next_date"] is None

    archive = (await client.get("/briefings")).json()
    assert [b["date"] for b in archive] == ["2026-10-09", "2026-10-08"]
    assert (await client.get("/briefings/2026-10-10")).status_code == 404  # a draft


# --- Runs ------------------------------------------------------------------------------


async def test_run_details_and_events_are_public_or_admin(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await _published(session, dt.date(2026, 10, 9), status=BriefingStatus.PUBLISHED)
    run.trace_url = "https://smith.example/trace"

    @asynccontextmanager
    async def sessions() -> AsyncGenerator[AsyncSession]:
        yield session

    log = EventLog(sessions, run.id)  # pyright: ignore[reportArgumentType]
    await log.record(
        (),
        "updates",
        {
            "merge_and_rank": {
                "stories": [1, 2, 3],
                "stats": RunStats(timings_ms={"merge_and_rank": 5}),
            }
        },
    )
    await log.record(
        ("reporter:abc",), "custom", {"event": "reporter.fetching", "beat": "releases"}
    )
    await log.record(
        ("fact_check:x",),
        "custom",
        {"event": "fact_check.story", "public": False, "headline": "Draft"},
    )

    public_run = (await client.get(f"/runs/{run.id}")).json()
    assert "cost_usd" not in public_run["stats"] and public_run["trace_url"] is None
    admin_run = (await client.get(f"/runs/{run.id}", headers=ADMIN)).json()
    assert admin_run["stats"]["cost_usd"] == 0.05 and admin_run["trace_url"]

    public = (await client.get(f"/runs/{run.id}/events")).json()
    assert [e["kind"] for e in public] == ["node", "progress"]
    assert public[0]["payload"] == {
        "branch": None,
        "node": "merge_and_rank",
        "stories": 3,
        "timings_ms": {"merge_and_rank": 5},
    }
    assert public[1]["payload"] == {"branch": "reporter:abc", "beat": "releases"}
    admin = (await client.get(f"/runs/{run.id}/events", headers=ADMIN)).json()
    assert len(admin) == 4 and admin[-1]["payload"]["headline"] == "Draft"
    newer = (await client.get(f"/runs/{run.id}/events?after={public[0]['id']}")).json()
    assert [e["id"] for e in newer] == [public[1]["id"]]


async def test_starting_a_run_needs_the_token_and_never_duplicates(
    client: httpx.AsyncClient, session: AsyncSession, dispatched: list[dict[str, str]]
) -> None:
    assert (await client.post("/runs", json={})).status_code == 401
    assert (
        await client.post("/runs", json={}, headers={"Authorization": "Bearer nope"})
    ).status_code == 401

    accepted = await client.post("/runs", json={}, headers=ADMIN)
    assert accepted.status_code == 202
    assert dispatched == [
        {"command": "run", "force": "false", "resend": "false", "review_mode": "auto"}
    ]

    today = dt.datetime.now(dt.UTC).date()
    active = await create_run(session, run_date=today, trigger=RunTrigger.MANUAL)
    conflict = await client.post("/runs", json={}, headers=ADMIN)
    assert conflict.status_code == 409 and conflict.json()["detail"]["run_id"] == str(active.id)

    active.status = RunStatus.PUBLISHED
    await session.flush()
    assert (await client.post("/runs", json={}, headers=ADMIN)).status_code == 409
    forced = await client.post("/runs", json={"force": True}, headers=ADMIN)
    assert forced.status_code == 202 and dispatched[-1]["force"] == "true"
    assert len(dispatched) == 2


async def test_a_review_is_recorded_and_the_run_resumed(
    client: httpx.AsyncClient, session: AsyncSession, dispatched: list[dict[str, str]]
) -> None:
    run = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)
    decision = {"action": "edit", "removed_story_ids": ["s1"]}
    assert (
        await client.post(f"/runs/{run.id}/review", json=decision, headers=ADMIN)
    ).status_code == 409

    run.status = RunStatus.AWAITING_REVIEW
    await session.flush()
    assert (await client.post(f"/runs/{run.id}/review", json=decision)).status_code == 401
    response = await client.post(f"/runs/{run.id}/review", json=decision, headers=ADMIN)
    assert response.status_code == 202
    await session.refresh(run)
    assert run.status == RunStatus.RESUME_REQUESTED
    assert run.review_decision is not None and run.review_decision["removed_story_ids"] == ["s1"]
    assert run.review_decision["decided_by"] == "human"
    assert dispatched == [{"command": "resume", "run_id": str(run.id)}]


# --- Feedback, settings, CORS ----------------------------------------------------------


async def test_feedback_nudges_the_nearest_interests_and_a_changed_vote_reverses(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await create_run(session, run_date=dt.date(2026, 10, 9), trigger=RunTrigger.MANUAL)
    text = "LangGraph adds durable retries for agents"
    story = db.Story(
        run_id=run.id,
        run_date=run.run_date,
        beat="releases",  # pyright: ignore[reportArgumentType]
        headline=text,
        summary=text,
        why_it_matters="x",
        embedding=embed_text(text),
    )
    near = db.Interest(
        topic="Agents",
        description="d",
        weight=1.0,
        embedding=embed_text("LangGraph durable retries agents"),
    )
    far = db.Interest(
        topic="Cooking", description="d", weight=1.0, embedding=embed_text("pasta tomato basil")
    )
    session.add_all([story, near, far])
    await session.flush()

    up = (
        await client.post(f"/stories/{story.id}/feedback", json={"value": 1}, headers=ADMIN)
    ).json()
    assert [i["topic"] for i in up["interests"]] == ["Agents"]
    raised = up["interests"][0]["weight"]
    assert raised > 1.0
    again = (
        await client.post(f"/stories/{story.id}/feedback", json={"value": 1}, headers=ADMIN)
    ).json()
    assert again["interests"] == []  # the same vote changes nothing
    down = (
        await client.post(f"/stories/{story.id}/feedback", json={"value": -1}, headers=ADMIN)
    ).json()
    assert down["interests"][0]["weight"] == pytest.approx(1.0 - (raised - 1.0), abs=1e-3)
    assert (
        await session.scalar(select(db.Feedback.value).where(db.Feedback.story_id == story.id))
    ) == -1
    assert (
        await client.post(f"/stories/{story.id}/feedback", json={"value": 1})
    ).status_code == 401


async def test_settings_need_the_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/settings")).status_code == 401
    body = {"interests": [{"topic": "MCP", "description": "The Model Context Protocol"}]}
    assert (await client.put("/settings", json=body, headers=ADMIN)).json()["interests"] == 1
    current = (await client.get("/settings", headers=ADMIN)).json()
    assert [i["topic"] for i in current["interests"]] == ["MCP"]


async def test_cors_allows_only_the_web_app(client: httpx.AsyncClient) -> None:
    allowed = await client.get("/health", headers={"Origin": ORIGIN})
    assert allowed.headers.get("access-control-allow-origin") == ORIGIN
    other = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_public_summaries_carry_no_text() -> None:
    summary = public_summary(
        {
            "draft": Briefing(
                date=dt.date(2026, 10, 9),
                intro="secret",
                stories=[],
                light_day=True,
                stats=RunStats(),
            ),
            "published": True,
            "assignments": [1, 2],
        }
    )
    assert summary == {"draft": "set", "published": True, "assignments": 2}
