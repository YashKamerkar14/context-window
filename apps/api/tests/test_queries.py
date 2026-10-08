import datetime as dt
import math

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import EMBEDDING_DIM, Item, Run, Source, Story
from context_window.db.queries import (
    NewItem,
    RunAlreadyActiveError,
    consumed_keys,
    create_run,
    find_similar_stories,
    mark_items_consumed,
    published_run_for,
    reportable_items,
    save_items,
    stale_runs,
    touch_heartbeat,
)
from context_window.schemas.enums import Beat, RunStatus, RunTrigger, SourceKind, StoryStatus

TODAY = dt.date(2026, 10, 8)


async def _source(session: AsyncSession, name: str = "Feed", beat: Beat = Beat.INDUSTRY) -> int:
    source = Source(
        name=name,
        beat=beat,
        kind=SourceKind.RSS,
        url=f"https://{name.lower()}.example/feed",
        authority=3,
    )
    session.add(source)
    await session.flush()
    return source.id


async def _now(session: AsyncSession) -> dt.datetime:
    return (await session.execute(text("SELECT now()"))).scalar_one()


# --- save_items --------------------------------------------------------------------------


async def test_save_items_inserts_and_returns_keys(session: AsyncSession) -> None:
    source_id = await _source(session)
    saved = await save_items(
        session,
        source_id,
        [
            NewItem(url="https://arxiv.org/abs/2610.03799v2", title=" A paper "),
            NewItem(url="https://example.com/post?utm_source=rss", title="A post"),
        ],
    )
    assert [s.canonical_key for s in saved] == ["arxiv:2610.03799", "https://example.com/post"]
    assert all(s.inserted for s in saved)
    stored = (await session.scalars(select(Item).order_by(Item.id))).all()
    assert stored[0].title == "A paper"
    assert stored[1].canonical_url == "https://example.com/post"


async def test_save_items_deduplicates_within_a_batch(session: AsyncSession) -> None:
    source_id = await _source(session)
    saved = await save_items(
        session,
        source_id,
        [
            NewItem(url="https://example.com/a", title="first"),
            NewItem(url="http://www.example.com/a/?utm_campaign=x", title="same page"),
        ],
    )
    assert len(saved) == 1
    assert (await session.scalar(select(Item.title))) == "first"


async def test_saving_again_keeps_the_first_fetch_time(session: AsyncSession) -> None:
    source_id = await _source(session)
    [first] = await save_items(
        session, source_id, [NewItem(url="https://example.com/a", title="v1")]
    )
    await session.execute(update(Item).values(fetched_at=Item.fetched_at - dt.timedelta(hours=5)))
    [again] = await save_items(
        session, source_id, [NewItem(url="https://example.com/a", title="v2 edited")]
    )
    assert again.id == first.id
    assert not again.inserted
    assert again.fetched_at == first.fetched_at - dt.timedelta(hours=5)
    assert (await session.scalar(select(Item.title))) == "v2 edited"


async def test_same_url_on_two_sources_is_two_items_with_one_key(session: AsyncSession) -> None:
    blog = await _source(session, "Blog")
    hn = await _source(session, "HN", Beat.COMMUNITY)
    [a] = await save_items(session, blog, [NewItem(url="https://openai.com/news/x", title="x")])
    [b] = await save_items(session, hn, [NewItem(url="https://openai.com/news/x/", title="x")])
    assert a.id != b.id
    assert a.canonical_key == b.canonical_key


async def test_explicit_key_overrides_url_key(session: AsyncSession) -> None:
    source_id = await _source(session)
    [saved] = await save_items(
        session,
        source_id,
        [NewItem(url="https://api.github.com/repos/a/b/releases/1", title="r", key="gh:a/b@v1")],
    )
    assert saved.canonical_key == "gh:a/b@v1"


async def test_excerpts_are_trimmed_to_500_characters(session: AsyncSession) -> None:
    source_id = await _source(session)
    await save_items(
        session, source_id, [NewItem(url="https://e.com/a", title="t", excerpt="word  " * 400)]
    )
    excerpt = await session.scalar(select(Item.excerpt))
    assert excerpt is not None
    assert len(excerpt) == 500
    assert excerpt.endswith("…")
    assert "  " not in excerpt


async def test_save_nothing(session: AsyncSession) -> None:
    assert await save_items(session, await _source(session), []) == []


# --- consumed and reportable -------------------------------------------------------------


async def test_consumed_key_on_one_source_blocks_it_everywhere(session: AsyncSession) -> None:
    blog = await _source(session, "Blog")
    hn = await _source(session, "HN", Beat.COMMUNITY)
    [from_blog] = await save_items(session, blog, [NewItem(url="https://e.com/x", title="x")])
    [from_hn] = await save_items(session, hn, [NewItem(url="https://e.com/x", title="x")])

    now = await _now(session)
    assert await mark_items_consumed(session, [from_blog.id], at=now) == 1
    assert await mark_items_consumed(session, [from_blog.id], at=now) == 0  # already consumed

    assert await consumed_keys(session, ["https://e.com/x", "https://e.com/other"]) == {
        "https://e.com/x"
    }
    assert await reportable_items(session, [from_hn], now=now) == []


async def test_reportable_items_age_out_after_72_hours(session: AsyncSession) -> None:
    source_id = await _source(session)
    saved = await save_items(
        session,
        source_id,
        [
            NewItem(url="https://e.com/fresh", title="f"),
            NewItem(url="https://e.com/old", title="o"),
        ],
    )
    now = await _now(session)
    keep = await reportable_items(session, saved, now=now + dt.timedelta(hours=71))
    assert len(keep) == 2
    gone = await reportable_items(session, saved, now=now + dt.timedelta(hours=73))
    assert gone == []


# --- similar stories ---------------------------------------------------------------------


def _unit(angle_degrees: float) -> list[float]:
    """A unit vector in the first two dimensions; cosine similarity = cos(angle difference)."""
    radians = math.radians(angle_degrees)
    vector = [0.0] * EMBEDDING_DIM
    vector[0], vector[1] = math.cos(radians), math.sin(radians)
    return vector


async def _story(
    session: AsyncSession,
    run: Run,
    headline: str,
    angle: float,
    status: StoryStatus,
    days_ago: int = 0,
) -> None:
    session.add(
        Story(
            run_id=run.id,
            run_date=TODAY - dt.timedelta(days=days_ago),
            beat=Beat.INDUSTRY,
            headline=headline,
            summary="s",
            why_it_matters="w",
            status=status,
            embedding=_unit(angle),
        )
    )
    await session.flush()


async def test_find_similar_stories(session: AsyncSession) -> None:
    run = await create_run(session, run_date=TODAY, trigger=RunTrigger.MANUAL)
    await _story(session, run, "same", 0, StoryStatus.PUBLISHED)
    await _story(session, run, "close", 20, StoryStatus.PUBLISHED)  # cos 20° ≈ 0.94
    await _story(session, run, "far", 60, StoryStatus.PUBLISHED)  # cos 60° = 0.5
    await _story(session, run, "dropped", 0, StoryStatus.DROPPED)
    await _story(session, run, "too old", 0, StoryStatus.PUBLISHED, days_ago=20)

    found = await find_similar_stories(
        session, _unit(0), since=TODAY - dt.timedelta(days=14), min_similarity=0.88
    )
    assert [s.headline for s in found] == ["same", "close"]
    assert found[0].similarity == pytest.approx(1.0)
    assert found[1].similarity == pytest.approx(math.cos(math.radians(20)), abs=1e-6)


async def test_find_similar_rejects_bad_threshold(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await find_similar_stories(session, _unit(0), since=TODAY, min_similarity=1.5)


# --- runs ----------------------------------------------------------------------------------


async def test_only_one_active_run_per_date(session: AsyncSession) -> None:
    first = await create_run(session, run_date=TODAY, trigger=RunTrigger.SCHEDULE)
    assert first.status is RunStatus.RUNNING
    assert first.id.version == 7

    with pytest.raises(RunAlreadyActiveError):
        await create_run(session, run_date=TODAY, trigger=RunTrigger.MANUAL)

    # The session is still usable after the rejected insert, and a failed run frees the date.
    first.status = RunStatus.FAILED
    await session.flush()
    second = await create_run(session, run_date=TODAY, trigger=RunTrigger.MANUAL)
    assert second.id != first.id


async def test_published_run_does_not_block_a_forced_rerun(session: AsyncSession) -> None:
    run = await create_run(session, run_date=TODAY, trigger=RunTrigger.SCHEDULE)
    run.status = RunStatus.PUBLISHED
    await session.flush()
    assert (await published_run_for(session, TODAY)) is not None
    rerun = await create_run(session, run_date=TODAY, trigger=RunTrigger.MANUAL)
    assert rerun.status is RunStatus.RUNNING


async def test_stale_runs(session: AsyncSession) -> None:
    alive = await create_run(session, run_date=TODAY, trigger=RunTrigger.SCHEDULE)
    dead = await create_run(
        session, run_date=TODAY - dt.timedelta(days=1), trigger=RunTrigger.SCHEDULE
    )
    await session.execute(
        update(Run)
        .where(Run.id == dead.id)
        .values(heartbeat_at=Run.heartbeat_at - dt.timedelta(minutes=10))
    )
    await touch_heartbeat(session, alive.id)

    stale = await stale_runs(session, older_than=dt.timedelta(minutes=2))
    assert [run.id for run in stale] == [dead.id]
