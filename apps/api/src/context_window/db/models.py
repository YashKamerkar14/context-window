"""ORM models for every table in PLAN.md section 5.

LangGraph's checkpointer owns its own `checkpoint*` tables; they are not modelled here.
"""

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from context_window.config import get_settings
from context_window.db.base import Base, str_enum
from context_window.ids import uuid7
from context_window.schemas.enums import (
    ACTIVE_RUN_STATUSES,
    Beat,
    BriefingStatus,
    DropReason,
    EvidenceKind,
    RunStatus,
    RunTrigger,
    SourceKind,
    StoryStatus,
    Verdict,
)

# The vector width is a deployment constant: changing it needs a migration and a re-embed.
EMBEDDING_DIM = get_settings().embedding_dim

Timestamp = DateTime(timezone=True)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(Timestamp, server_default=func.now())


def _updated_at() -> Mapped[dt.datetime]:
    return mapped_column(Timestamp, server_default=func.now(), onupdate=func.now())


class Source(Base):
    """A feed or API that a beat reads from."""

    __tablename__ = "sources"
    __table_args__ = (CheckConstraint("authority BETWEEN 1 AND 5", name="authority_range"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    beat: Mapped[Beat] = mapped_column(str_enum(Beat, "beat"))
    kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind, "source_kind"))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text, unique=True)
    authority: Mapped[int] = mapped_column(SmallInteger)
    # Per-source fetch options, e.g. arXiv categories or the Hacker News points threshold.
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(server_default=sql_text("true"))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class WatchlistRepo(Base):
    """A GitHub repository tracked by the releases beat."""

    __tablename__ = "watchlist_repos"
    __table_args__ = (UniqueConstraint("owner", "repo"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    repo: Mapped[str] = mapped_column(String(100))
    pypi_package: Mapped[str | None] = mapped_column(String(200))
    # Python regex a release tag must fully match. Filters monorepos with many packages.
    tag_pattern: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(server_default=sql_text("true"))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class Interest(Base):
    """A topic the reader cares about. `weight` is adjusted by feedback."""

    __tablename__ = "interests"
    __table_args__ = (CheckConstraint("weight BETWEEN 0.2 AND 2.0", name="weight_range"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(String(100), unique=True)
    # The text that gets embedded; richer than the short topic label.
    description: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, server_default=sql_text("1.0"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class Item(Base):
    """A raw item fetched from a source. Only short excerpts are stored, never full text."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("source_id", "canonical_url"),
        Index(None, "canonical_key"),
        Index(None, "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    canonical_key: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[dt.datetime] = mapped_column(Timestamp, server_default=func.now())
    # Set only when a story built from this item is published.
    consumed_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)


class Run(Base):
    """One newsroom run."""

    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_run_date_active",
            "run_date",
            unique=True,
            postgresql_where=sql_text(
                "status IN (" + ", ".join(f"'{s.value}'" for s in ACTIVE_RUN_STATUSES) + ")"
            ),
        ),
        Index(None, "status", "heartbeat_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_date: Mapped[dt.date] = mapped_column(Date)
    trigger: Mapped[RunTrigger] = mapped_column(str_enum(RunTrigger, "run_trigger"))
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "run_status"), server_default=RunStatus.RUNNING.value
    )
    started_at: Mapped[dt.datetime] = mapped_column(Timestamp, server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    actions_run_url: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    trace_url: Mapped[str | None] = mapped_column(Text)
    # Human review: when the run paused for it, and the decision that resumes it.
    review_requested_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    review_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RunEvent(Base):
    """Event log for the live newsroom view. `public` events are safe to show anyone."""

    __tablename__ = "run_events"
    __table_args__ = (Index(None, "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(Timestamp, server_default=func.now())
    node: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(50))
    public: Mapped[bool] = mapped_column(server_default=sql_text("false"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))


class Story(Base):
    """A story produced by a run. Published stories form the 14-day dedupe memory."""

    __tablename__ = "stories"
    __table_args__ = (
        Index(None, "status", "run_date"),
        Index(None, "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"))
    run_date: Mapped[dt.date] = mapped_column(Date)
    beat: Mapped[Beat] = mapped_column(str_enum(Beat, "beat"))
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    is_breaking_change: Mapped[bool] = mapped_column(server_default=sql_text("false"))
    published_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[StoryStatus] = mapped_column(
        str_enum(StoryStatus, "story_status"), server_default=StoryStatus.CANDIDATE.value
    )
    drop_reason: Mapped[DropReason | None] = mapped_column(str_enum(DropReason, "drop_reason"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class StorySource(Base):
    __tablename__ = "story_sources"

    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), primary_key=True, index=True
    )


class Claim(Base):
    """A checkable claim in a story, with the fact-checker's verdict."""

    __tablename__ = "claims"
    __table_args__ = (CheckConstraint("attempts >= 0", name="attempts_non_negative"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    verdict: Mapped[Verdict | None] = mapped_column(str_enum(Verdict, "verdict"))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(String(300))
    evidence_kind: Mapped[EvidenceKind | None] = mapped_column(
        str_enum(EvidenceKind, "evidence_kind")
    )
    reasoning: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=sql_text("0"))
    checked_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)


class Briefing(Base):
    """The published output. One per date."""

    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, unique=True)
    status: Mapped[BriefingStatus] = mapped_column(
        str_enum(BriefingStatus, "briefing_status"), server_default=BriefingStatus.DRAFT.value
    )
    body_markdown: Mapped[str] = mapped_column(Text)
    emailed_at: Mapped[dt.datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class Feedback(Base):
    """One vote per story; changing a vote updates the row."""

    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("value IN (-1, 1)", name="value_is_vote"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), unique=True
    )
    value: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


class FetchCache(Base):
    """Fetched pages for one run, shared across MCP sessions. Purged after 7 days."""

    __tablename__ = "fetch_cache"
    __table_args__ = (Index(None, "fetched_at"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    body: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(Timestamp, server_default=func.now())
