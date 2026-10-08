"""The migration creates exactly what the models describe, and runs both ways."""

import secrets
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text

from context_window import aio
from context_window.db.base import Base
from context_window.db.migrate import (
    DbStatus,
    EmbeddingDimMismatchError,
    alembic_config,
    check_embedding_dim,
    db_status,
    setup_checkpointer,
)
from context_window.db.schema_filter import include_object
from context_window.db.session import make_engine
from tests.conftest import with_search_path

APP_TABLES = {
    "sources",
    "watchlist_repos",
    "interests",
    "items",
    "runs",
    "run_events",
    "stories",
    "story_sources",
    "claims",
    "briefings",
    "feedback",
    "fetch_cache",
}
CHECKPOINT_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


def _tables(conn: Connection, schema: str) -> set[str]:
    # Explicit schema: without it, Postgres lists every table visible through the
    # search_path, so same-named tables in `public` would hide a dropped test table.
    return set(inspect(conn).get_table_names(schema=schema))


def _diff(conn: Connection) -> list[Any]:
    context = MigrationContext.configure(
        conn,
        opts={
            "include_object": include_object,
            "compare_type": True,
            "compare_server_default": True,
        },
    )
    return compare_metadata(context, Base.metadata)


@pytest.fixture(scope="module")
def scratch_schema(database_url: str) -> Any:
    """A separate empty schema, so this module can migrate down and up freely."""
    schema = f"test_{secrets.token_hex(6)}"

    async def run(sql: str) -> None:
        engine = make_engine(database_url, null_pool=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    aio.run(run(f'CREATE SCHEMA "{schema}"'))
    try:
        yield schema
    finally:
        aio.run(run(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


async def test_upgrade_creates_every_table_and_matches_the_models(
    database_url: str, scratch_schema: str
) -> None:
    # The checkpointer's tables live alongside ours; autogenerate must ignore them.
    await setup_checkpointer(with_search_path(database_url, scratch_schema))

    engine = make_engine(database_url, search_path=scratch_schema, null_pool=True)
    try:
        async with engine.begin() as conn:

            def upgrade_and_check(sync_conn: Connection) -> None:
                cfg = alembic_config(connection=sync_conn, schema=scratch_schema)
                command.upgrade(cfg, "head")
                tables = _tables(sync_conn, scratch_schema)
                assert tables >= APP_TABLES | CHECKPOINT_TABLES
                assert _diff(sync_conn) == []

            await conn.run_sync(upgrade_and_check)
            status = await db_status(conn)
            assert status.revision == "0001"
            assert status.pgvector_version is not None
            assert status.embedding_column_type == "vector(1536)"

        async with engine.begin() as conn:

            def downgrade_then_upgrade(sync_conn: Connection) -> None:
                cfg = alembic_config(connection=sync_conn, schema=scratch_schema)
                command.downgrade(cfg, "base")
                tables = _tables(sync_conn, scratch_schema)
                assert not tables & APP_TABLES
                assert tables >= CHECKPOINT_TABLES  # untouched by our migrations
                command.upgrade(cfg, "head")
                assert _tables(sync_conn, scratch_schema) >= APP_TABLES

            await conn.run_sync(downgrade_then_upgrade)
    finally:
        await engine.dispose()


async def test_partial_unique_index_only_covers_active_runs(
    database_url: str, migrated_schema: str
) -> None:
    engine = make_engine(database_url, search_path=migrated_schema, null_pool=True)
    try:
        async with engine.connect() as conn:
            definition = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND indexname = 'uq_runs_run_date_active'"
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert "UNIQUE" in definition
    for status in ("queued", "running", "awaiting_review", "resume_requested"):
        assert status in definition
    assert "published" not in definition


def test_embedding_dim_mismatch_is_reported() -> None:
    status = DbStatus("18", "0001", "0.8.6", "vector(1536)")
    check_embedding_dim(status, 1536)
    with pytest.raises(EmbeddingDimMismatchError, match="EMBEDDING_DIM=768"):
        check_embedding_dim(status, 768)


def test_embedding_dim_check_skips_an_unmigrated_database() -> None:
    check_embedding_dim(DbStatus("18", None, None, None), 768)
