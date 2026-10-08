"""Programmatic migrations and database setup (`cw db init`)."""

import logging
from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection

from context_window.config import get_settings
from context_window.db.session import make_engine

log = logging.getLogger(__name__)

SCRIPT_LOCATION = "context_window.db:migrations"


def alembic_config(
    *,
    url: str | None = None,
    connection: Connection | None = None,
    schema: str | None = None,
) -> Config:
    """Alembic config without an ini file. Values go through `attributes`, never through
    ini interpolation, so passwords containing `%` are safe."""
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    if url is not None:
        cfg.attributes["url"] = url
    if connection is not None:
        cfg.attributes["connection"] = connection
    if schema is not None:
        cfg.attributes["schema"] = schema
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Run migrations up to `revision`. Synchronous: Alembic starts its own event loop."""
    command.upgrade(alembic_config(url=url), revision)


async def setup_checkpointer(url: str) -> None:
    """Create or migrate LangGraph's checkpoint tables. Idempotent."""
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()


class EmbeddingDimMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class DbStatus:
    server_version: str
    revision: str | None
    pgvector_version: str | None
    embedding_column_type: str | None


async def db_status(conn: AsyncConnection) -> DbStatus:
    server_version = (await conn.execute(text("SHOW server_version"))).scalar_one()
    revision = None
    if (await conn.execute(text("SELECT to_regclass('alembic_version')"))).scalar_one():
        revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    pgvector_version = (
        await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    ).scalar_one_or_none()
    column_type = (
        await conn.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = to_regclass('stories') AND attname = 'embedding' "
                "AND NOT attisdropped"
            )
        )
    ).scalar_one_or_none()
    return DbStatus(str(server_version), revision, pgvector_version, column_type)


def check_embedding_dim(status: DbStatus, expected_dim: int) -> None:
    """Fail loudly when EMBEDDING_DIM no longer matches the database columns."""
    expected = f"vector({expected_dim})"
    if status.embedding_column_type is not None and status.embedding_column_type != expected:
        raise EmbeddingDimMismatchError(
            f"stories.embedding is {status.embedding_column_type} but EMBEDDING_DIM="
            f"{expected_dim}. Changing the embedding width needs a migration and a re-embed."
        )


async def inspect(url: str) -> DbStatus:
    engine = make_engine(url, null_pool=True)
    try:
        async with engine.connect() as conn:
            return await db_status(conn)
    finally:
        await engine.dispose()


def init_db() -> DbStatus:
    """Migrate to the latest revision, set up the checkpointer and verify the result."""
    from context_window import aio

    settings = get_settings()
    url = settings.require_direct_url()
    log.info("running migrations")
    upgrade(url)
    log.info("setting up the LangGraph checkpointer")
    aio.run(setup_checkpointer(url))
    status = aio.run(inspect(url))
    check_embedding_dim(status, settings.embedding_dim)
    return status
