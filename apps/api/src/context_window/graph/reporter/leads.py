"""What every desk shares: fetched items, leads, saving, the reporter filter, and turning
the writer's drafts into candidates.

A desk fetches with deterministic code, saves every item, and returns leads: one per
thing worth writing about, each with its primary source URL (PLAN.md 6.6), all its
source URLs and canonical keys, and the text the writer works from.
"""

import datetime as dt
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.queries import (
    NewItem,
    SavedItem,
    consumed_keys,
    reportable_items,
    save_items,
)
from context_window.graph.reporter.drafts import DraftStory
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import Claim, RunError, SentenceClaims, StoryCandidate

# How far back desks fetch. It matches the reporter filter's 72-hour age limit: anything
# older could not be reported anyway.
LOOKBACK = dt.timedelta(hours=72)
# About 1.5k tokens of text per lead for the writer (PLAN.md 6.3).
WRITER_TEXT_CHARS = 6000


@dataclass(frozen=True)
class Fetched:
    """One raw item from one source, as fetched."""

    source_id: int
    authority: int
    url: str
    key: str
    title: str
    published_at: dt.datetime | None
    excerpt: str = ""


class Lead(BaseModel):
    """One thing to write about, small enough for the reporter's checkpointed state. The
    text the writer works from is in the run's fetch cache under `url`."""

    beat: Beat
    title: str
    url: str = Field(description="The primary source: claims are checked against it")
    source_urls: list[str]
    canonical_keys: list[str]
    published_at: dt.datetime | None
    authority: int = Field(ge=1, le=5)
    has_text: bool
    # Context for the writer, e.g. the repo and version of a release.
    labels: dict[str, str] = Field(default_factory=dict[str, str])


@dataclass
class DeskResult:
    leads: list[Lead]
    texts: dict[str, str]  # lead.url -> the text the writer works from
    fetched: int
    errors: list[RunError] = field(default_factory=list[RunError])


def _new_item(item: Fetched) -> NewItem:
    excerpt = item.excerpt.strip()
    return NewItem(
        url=item.url,
        title=item.title,
        published_at=item.published_at,
        excerpt=excerpt or None,
        content_hash=hashlib.sha256(excerpt.encode()).hexdigest() if excerpt else None,
        key=item.key,
    )


async def save_fetched(session: AsyncSession, items: Sequence[Fetched]) -> dict[str, SavedItem]:
    """Save every fetched item; return them all by canonical key."""
    by_source: dict[int, list[Fetched]] = {}
    for item in items:
        by_source.setdefault(item.source_id, []).append(item)
    saved: dict[str, SavedItem] = {}
    for source_id, batch in by_source.items():
        for row in await save_items(session, source_id, [_new_item(i) for i in batch]):
            saved[row.canonical_key] = row
    return saved


async def reportable_leads(
    session: AsyncSession,
    leads: Sequence[Lead],
    saved: dict[str, SavedItem],
    *,
    now: dt.datetime,
    max_items: int | None,
) -> list[Lead]:
    """The reporter filter, per lead: none of its keys consumed, and its primary item first
    fetched less than 72 hours ago. Newest first, at most `max_items` (None: no cap)."""
    primaries = [saved[lead.canonical_keys[0]] for lead in leads if lead.canonical_keys[0] in saved]
    fresh = {item.canonical_key for item in await reportable_items(session, primaries, now=now)}
    consumed = await consumed_keys(session, (key for lead in leads for key in lead.canonical_keys))
    kept = [
        lead
        for lead in leads
        if lead.canonical_keys[0] in fresh and not consumed.intersection(lead.canonical_keys)
    ]
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    kept.sort(key=lambda lead: lead.published_at or epoch, reverse=True)
    return kept if max_items is None else kept[:max_items]


def _clean(text: str) -> str:
    return " ".join(text.split())


def to_candidate(draft: DraftStory, lead: Lead) -> StoryCandidate:
    """A validated candidate. URLs and keys come from the lead, never from the model.
    Raises pydantic.ValidationError when the draft breaks the candidate's rules."""
    sentences = [
        SentenceClaims(sentence=_clean(s.text), claim_ids=[c.strip() for c in s.claim_ids])
        for s in draft.summary
    ]
    return StoryCandidate(
        beat=lead.beat,
        headline=_clean(draft.headline),
        summary=" ".join(s.sentence for s in sentences),
        why_it_matters=_clean(draft.why_it_matters),
        claims=[
            Claim(id=c.id.strip(), text=_clean(c.text), source_url=lead.url) for c in draft.claims
        ],
        sentence_claim_map=sentences,
        source_urls=lead.source_urls,
        canonical_keys=lead.canonical_keys,
        published_at=lead.published_at,
        # Only release notes can say a change is breaking.
        is_breaking_change=draft.is_breaking_change and lead.beat is Beat.RELEASES,
    )
