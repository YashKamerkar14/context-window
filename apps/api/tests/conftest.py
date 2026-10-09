"""Shared fixtures.

Database tests run against TEST_DATABASE_URL, or the direct DATABASE_URL_DIRECT from .env,
inside a throwaway schema: the session migrates a fresh `test_<random>` schema, each test
runs in a transaction that is rolled back, and the schema is dropped at the end. Set
REQUIRE_DB_TESTS=1 (as CI does) to fail instead of skip when no database is configured.
"""

import asyncio
import os
import secrets
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

from context_window import aio
from context_window.config import get_settings
from context_window.db.migrate import alembic_config
from context_window.db.session import make_engine

if sys.platform == "win32":

    def pytest_asyncio_loop_factories(
        config: pytest.Config, item: pytest.Item
    ) -> Mapping[str, object]:
        # psycopg's async mode cannot use Windows' default ProactorEventLoop.
        return {"selector": asyncio.SelectorEventLoop}


class SecretUrl(str):
    """A database URL that never shows its password in pytest tracebacks or logs."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<database url hidden>"

    __str__ = str.__str__


def _test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    direct = get_settings().database_url_direct
    return direct.get_secret_value() if direct else None


def with_search_path(url: str, schema: str) -> str:
    """Add a libpq `options` parameter so every connection starts in `schema`."""
    parts = urlsplit(url)
    option = "options=" + quote(f"-csearch_path={schema},public", safe="")
    query = f"{parts.query}&{option}" if parts.query else option
    return urlunsplit(parts._replace(query=query))


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if url is None:
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail("REQUIRE_DB_TESTS=1 but no TEST_DATABASE_URL or DATABASE_URL_DIRECT")
        pytest.skip("no database configured (set TEST_DATABASE_URL)")
    return SecretUrl(url)


def _upgrade(connection: Connection, schema: str) -> None:
    command.upgrade(alembic_config(connection=connection, schema=schema), "head")


async def _create_schema(url: str, schema: str) -> None:
    engine = make_engine(url, null_pool=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(_upgrade, schema)
    finally:
        await engine.dispose()


async def _drop_schema(url: str, schema: str) -> None:
    engine = make_engine(url, null_pool=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_schema(database_url: str) -> Iterator[str]:
    """A fresh schema migrated to head, dropped after the test session."""
    schema = f"test_{secrets.token_hex(6)}"
    aio.run(_create_schema(database_url, schema))
    try:
        yield schema
    finally:
        aio.run(_drop_schema(database_url, schema))


@pytest.fixture
async def session(database_url: str, migrated_schema: str) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back after the test, even if the test commits."""
    engine = make_engine(database_url, search_path=migrated_schema, null_pool=True)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            db = AsyncSession(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield db
            finally:
                await db.close()
                await outer.rollback()
    finally:
        await engine.dispose()


async def _setup_checkpointer(url: str) -> None:
    from context_window.graph.checkpoint import postgres_checkpointer

    async with postgres_checkpointer(url, max_size=1) as saver:
        await saver.setup()


@pytest.fixture(scope="session")
def checkpoint_tables(database_url: str, migrated_schema: str) -> str:
    """The checkpointer's tables in the test schema, created before any test transaction
    opens: their setup builds indexes CONCURRENTLY, which waits for every open transaction."""
    url = with_search_path(database_url, migrated_schema)
    aio.run(_setup_checkpointer(url))
    return url
