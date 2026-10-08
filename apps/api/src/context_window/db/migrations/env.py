"""Alembic environment.

Runs against a connection handed over in `config.attributes["connection"]` (programmatic
use and tests), or otherwise opens one from the direct database URL.
"""

from alembic import context
from sqlalchemy import Connection

from context_window import aio
from context_window.config import get_settings

# Import the models so their tables are registered on Base.metadata.
from context_window.db import models  # noqa: F401  # pyright: ignore[reportUnusedImport]
from context_window.db.base import Base
from context_window.db.schema_filter import include_object
from context_window.db.session import make_engine, validate_schema_name

config = context.config
target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    schema: str | None = config.attributes.get("schema")
    if schema is not None:
        validate_schema_name(schema)
        connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        version_table_schema=schema,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url: str = config.attributes.get("url") or get_settings().require_direct_url()
    engine = make_engine(url, null_pool=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is None:
        aio.run(run_async_migrations())
    else:
        do_run_migrations(connection)


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported; run against a database.")
run_migrations_online()
