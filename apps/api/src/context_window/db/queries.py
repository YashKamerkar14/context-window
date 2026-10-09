"""Query helpers used by the newsroom graph."""

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import Item, Run, Story
from context_window.memory.canonical import canonical_key, canonicalize_url
from context_window.schemas.enums import RunStatus, RunTrigger, StoryStatus

EXCERPT_MAX_CHARS = 500


# --- Items -----------------------------------------------------------------------------


@dataclass(frozen=True)
class NewItem:
    """An item as a reporter fetched it, before it is saved."""

    url: str
    title: str
    published_at: dt.datetime | None = None
    excerpt: str | None = None
    content_hash: str | None = None
    # Overrides the key derived from the URL, e.g. `gh:owner/repo@tag` from API data.
    key: str | None = None


@dataclass(frozen=True)
class SavedItem:
    id: int
    canonical_key: str
    fetched_at: dt.datetime
    inserted: bool


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    if len(text) <= EXCERPT_MAX_CHARS:
        return text
    return text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"


async def save_items(
    session: AsyncSession, source_id: int, items: Iterable[NewItem]
) -> list[SavedItem]:
    """Insert new items and return every item, new or already known, with its first fetch time.

    Items are matched on (source, canonical URL). Duplicates within the batch keep the first
    occurrence, because one INSERT ... ON CONFLICT cannot touch the same row twice.
    """
    rows: dict[str, dict[str, object]] = {}
    for item in items:
        canonical = canonicalize_url(item.url)
        if canonical in rows:
            continue
        rows[canonical] = {
            "source_id": source_id,
            "url": item.url,
            "canonical_url": canonical,
            "canonical_key": item.key or canonical_key(item.url),
            "title": item.title.strip(),
            "excerpt": _excerpt(item.excerpt),
            "published_at": item.published_at,
            "content_hash": item.content_hash,
        }
    if not rows:
        return []

    stmt = insert(Item).values(list(rows.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Item.source_id, Item.canonical_url],
        # Refresh what a source may have edited; keep fetched_at as the first-seen time.
        set_={
            "title": stmt.excluded.title,
            "excerpt": stmt.excluded.excerpt,
            "published_at": func.coalesce(stmt.excluded.published_at, Item.published_at),
            "content_hash": stmt.excluded.content_hash,
        },
    ).returning(
        Item.id,
        Item.canonical_key,
        Item.fetched_at,
        # xmax is 0 for a freshly inserted row and non-zero for an updated one.
        (literal_column("xmax") == literal_column("0")).label("inserted"),
    )
    result = await session.execute(stmt)
    return [
        SavedItem(
            id=r.id, canonical_key=r.canonical_key, fetched_at=r.fetched_at, inserted=r.inserted
        )
        for r in result
    ]


async def consumed_keys(session: AsyncSession, keys: Iterable[str]) -> set[str]:
    """Which of these keys already belong to a published story (on any source)."""
    key_list = sorted(set(keys))
    if not key_list:
        return set()
    stmt = (
        select(Item.canonical_key)
        .where(Item.canonical_key.in_(key_list), Item.consumed_at.is_not(None))
        .distinct()
    )
    return set((await session.scalars(stmt)).all())


async def reportable_items(
    session: AsyncSession,
    saved: Sequence[SavedItem],
    *,
    now: dt.datetime,
    max_age: dt.timedelta = dt.timedelta(hours=72),
) -> list[SavedItem]:
    """The reporter filter: keep items not yet consumed and first fetched within `max_age`.

    Unpublished items stay eligible until they age out, so a crashed or rejected run, or a
    story ranked just below the cut, does not lose them.
    """
    consumed = await consumed_keys(session, (item.canonical_key for item in saved))
    cutoff = now - max_age
    return [
        item for item in saved if item.canonical_key not in consumed and item.fetched_at >= cutoff
    ]


async def mark_items_consumed(
    session: AsyncSession, item_ids: Iterable[int], *, at: dt.datetime
) -> int:
    ids = sorted(set(item_ids))
    if not ids:
        return 0
    stmt = update(Item).where(Item.id.in_(ids), Item.consumed_at.is_(None)).values(consumed_at=at)
    result = cast(CursorResult[Any], await session.execute(stmt))
    return result.rowcount


async def consume_stories(
    session: AsyncSession, stories: Iterable["HasKeys"], *, at: dt.datetime
) -> int:
    """Mark the items behind these stories consumed, as publishing does. Returns the rows
    changed."""
    keys = {key for story in stories for key in story.canonical_keys}
    if not keys:
        return 0
    ids = await session.scalars(select(Item.id).where(Item.canonical_key.in_(keys)))
    return await mark_items_consumed(session, ids, at=at)


class HasKeys(Protocol):
    @property
    def canonical_keys(self) -> list[str]: ...


# --- Similar stories -------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarStory:
    id: uuid.UUID
    headline: str
    run_date: dt.date
    similarity: float


async def find_similar_stories(
    session: AsyncSession,
    embedding: Sequence[float],
    *,
    since: dt.date,
    min_similarity: float,
    statuses: Sequence[StoryStatus] = (StoryStatus.PUBLISHED,),
    limit: int = 5,
) -> list[SimilarStory]:
    """Stories since `since` whose cosine similarity to `embedding` is at least
    `min_similarity`, most similar first."""
    if not 0.0 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be between 0 and 1")
    distance = Story.embedding.cosine_distance(list(embedding))  # pyright: ignore[reportOptionalMemberAccess]
    similarity = (1 - distance).label("similarity")
    stmt = (
        select(Story.id, Story.headline, Story.run_date, similarity)
        .where(
            Story.embedding.is_not(None),
            Story.status.in_(list(statuses)),
            Story.run_date >= since,
            distance <= 1 - min_similarity,
        )
        .order_by(distance)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [
        SimilarStory(
            id=r.id, headline=r.headline, run_date=r.run_date, similarity=float(r.similarity)
        )
        for r in result
    ]


# --- Runs ------------------------------------------------------------------------------


class RunAlreadyActiveError(RuntimeError):
    def __init__(self, run_date: dt.date) -> None:
        super().__init__(f"a run for {run_date.isoformat()} is already in progress")
        self.run_date = run_date


async def create_run(
    session: AsyncSession,
    *,
    run_date: dt.date,
    trigger: RunTrigger,
    actions_run_url: str | None = None,
) -> Run:
    """Start a run for `run_date`. At most one run per date may be active at a time."""
    run = Run(
        run_date=run_date,
        trigger=trigger,
        status=RunStatus.RUNNING,
        actions_run_url=actions_run_url,
        heartbeat_at=func.now(),
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:
        if "uq_runs_run_date_active" in str(exc.orig):
            raise RunAlreadyActiveError(run_date) from exc
        raise
    await session.refresh(run)
    return run


async def published_run_for(session: AsyncSession, run_date: dt.date) -> Run | None:
    stmt = select(Run).where(Run.run_date == run_date, Run.status == RunStatus.PUBLISHED)
    return (await session.scalars(stmt.limit(1))).first()


async def touch_heartbeat(session: AsyncSession, run_id: uuid.UUID) -> None:
    await session.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=func.now()))


async def stale_runs(session: AsyncSession, *, older_than: dt.timedelta) -> list[Run]:
    """Runs marked running whose heartbeat stopped: their job died and they need resuming."""
    cutoff = func.now() - older_than
    stmt = (
        select(Run)
        .where(
            Run.status == RunStatus.RUNNING,
            func.coalesce(Run.heartbeat_at, Run.started_at) < cutoff,
        )
        .order_by(Run.started_at)
    )
    return list((await session.scalars(stmt)).all())
