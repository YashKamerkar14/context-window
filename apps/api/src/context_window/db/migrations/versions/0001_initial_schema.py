"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-08 00:45:26.519290
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from context_window.config import get_settings

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _embedding_dim() -> int:
    """The vector width comes from EMBEDDING_DIM at migration time (default 1536)."""
    return get_settings().embedding_dim


def upgrade() -> None:
    # Extensions are database-wide; pin pgvector to `public` so isolated schemas share it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table(
        "interests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("embedding", Vector(_embedding_dim()), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("weight BETWEEN 0.2 AND 2.0", name=op.f("ck_interests_weight_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interests")),
        sa.UniqueConstraint("topic", name=op.f("uq_interests_topic")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actions_run_url", sa.Text(), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("trace_url", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_review', 'resume_requested', 'published', 'rejected', 'expired', 'failed')",
            name=op.f("ck_runs_run_status"),
        ),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name=op.f("ck_runs_run_trigger")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(
        op.f("ix_runs_status_heartbeat_at"), "runs", ["status", "heartbeat_at"], unique=False
    )
    op.create_index(
        "uq_runs_run_date_active",
        "runs",
        ["run_date"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'awaiting_review', 'resume_requested')"
        ),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("beat", sa.String(length=9), nullable=False),
        sa.Column("kind", sa.String(length=15), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("authority", sa.SmallInteger(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "beat IN ('research', 'releases', 'industry', 'community')",
            name=op.f("ck_sources_beat"),
        ),
        sa.CheckConstraint(
            "kind IN ('rss', 'github_releases', 'pypi_releases', 'arxiv', 'hf_papers', 'hackernews')",
            name=op.f("ck_sources_source_kind"),
        ),
        sa.CheckConstraint("authority BETWEEN 1 AND 5", name=op.f("ck_sources_authority_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("url", name=op.f("uq_sources_url")),
    )
    op.create_table(
        "watchlist_repos",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("repo", sa.String(length=100), nullable=False),
        sa.Column("pypi_package", sa.String(length=200), nullable=True),
        sa.Column("tag_pattern", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_repos")),
        sa.UniqueConstraint("owner", "repo", name=op.f("uq_watchlist_repos_owner_repo")),
    )
    op.create_table(
        "briefings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=9), server_default="draft", nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')", name=op.f("ck_briefings_briefing_status")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_briefings_run_id_runs"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefings")),
        sa.UniqueConstraint("date", name=op.f("uq_briefings_date")),
    )
    op.create_index(op.f("ix_briefings_run_id"), "briefings", ["run_id"], unique=False)
    op.create_table(
        "fetch_cache",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_fetch_cache_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "url", name=op.f("pk_fetch_cache")),
    )
    op.create_index(op.f("ix_fetch_cache_fetched_at"), "fetch_cache", ["fetched_at"], unique=False)
    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("canonical_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.String(length=500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_items_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint(
            "source_id", "canonical_url", name=op.f("uq_items_source_id_canonical_url")
        ),
    )
    op.create_index(op.f("ix_items_canonical_key"), "items", ["canonical_key"], unique=False)
    op.create_index(op.f("ix_items_fetched_at"), "items", ["fetched_at"], unique=False)
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("node", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
    )
    op.create_index(op.f("ix_run_events_run_id_id"), "run_events", ["run_id", "id"], unique=False)
    op.create_table(
        "stories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("beat", sa.String(length=9), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column(
            "is_breaking_change", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=9), server_default="candidate", nullable=False),
        sa.Column("drop_reason", sa.String(length=17), nullable=True),
        sa.Column("embedding", Vector(_embedding_dim()), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "beat IN ('research', 'releases', 'industry', 'community')",
            name=op.f("ck_stories_beat"),
        ),
        sa.CheckConstraint(
            "drop_reason IN ('unsupported', 'unclear_only', 'fetch_failed', 'budget', 'removed_by_editor')",
            name=op.f("ck_stories_drop_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'verified', 'dropped', 'published')",
            name=op.f("ck_stories_story_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_stories_run_id_runs"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stories")),
    )
    op.create_index(op.f("ix_stories_run_id"), "stories", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_stories_status_run_date"), "stories", ["status", "run_date"], unique=False
    )
    op.create_table(
        "claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("story_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=11), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("evidence_quote", sa.String(length=300), nullable=True),
        sa.Column("evidence_kind", sa.String(length=4), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "evidence_kind IN ('page', 'feed')", name=op.f("ck_claims_evidence_kind")
        ),
        sa.CheckConstraint(
            "verdict IN ('supported', 'unsupported', 'unclear')", name=op.f("ck_claims_verdict")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_claims_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_claims_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_index(op.f("ix_claims_story_id"), "claims", ["story_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("story_id", sa.UUID(), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("value IN (-1, 1)", name=op.f("ck_feedback_value_is_vote")),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_feedback_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("story_id", name=op.f("uq_feedback_story_id")),
    )
    op.create_table(
        "story_sources",
        sa.Column("story_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_story_sources_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name=op.f("fk_story_sources_story_id_stories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("story_id", "item_id", name=op.f("pk_story_sources")),
    )
    op.create_index(op.f("ix_story_sources_item_id"), "story_sources", ["item_id"], unique=False)


def downgrade() -> None:
    # The pgvector extension is left installed: other schemas may use it.
    op.drop_index(op.f("ix_story_sources_item_id"), table_name="story_sources")
    op.drop_table("story_sources")
    op.drop_table("feedback")
    op.drop_index(op.f("ix_claims_story_id"), table_name="claims")
    op.drop_table("claims")
    op.drop_index(op.f("ix_stories_status_run_date"), table_name="stories")
    op.drop_index(op.f("ix_stories_run_id"), table_name="stories")
    op.drop_table("stories")
    op.drop_index(op.f("ix_run_events_run_id_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_items_fetched_at"), table_name="items")
    op.drop_index(op.f("ix_items_canonical_key"), table_name="items")
    op.drop_table("items")
    op.drop_index(op.f("ix_fetch_cache_fetched_at"), table_name="fetch_cache")
    op.drop_table("fetch_cache")
    op.drop_index(op.f("ix_briefings_run_id"), table_name="briefings")
    op.drop_table("briefings")
    op.drop_table("watchlist_repos")
    op.drop_table("sources")
    op.drop_index(
        "uq_runs_run_date_active",
        table_name="runs",
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'awaiting_review', 'resume_requested')"
        ),
    )
    op.drop_index(op.f("ix_runs_status_heartbeat_at"), table_name="runs")
    op.drop_table("runs")
    op.drop_table("interests")
