"""Engines and sessions.

Neon has two endpoints: a PgBouncer pooler (host contains `-pooler`) and a direct one.
Through the pooler, server-side prepared statements and startup options such as
`search_path` are not reliable, so both are switched off there.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def sqlalchemy_url(url: str) -> str:
    """Turn a libpq URL (as Neon gives it) into a SQLAlchemy psycopg URL."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError("database URL must look like postgresql://user:pass@host/db")
    if scheme in ("postgres", "postgresql"):
        return f"postgresql+psycopg://{rest}"
    if scheme == "postgresql+psycopg":
        return url
    raise ValueError(f"unsupported database URL scheme: {scheme!r}")


def is_pooled(url: str) -> bool:
    return "-pooler" in (urlsplit(url).hostname or "")


def validate_schema_name(schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    return schema


def make_engine(
    url: str,
    *,
    search_path: str | None = None,
    null_pool: bool = False,
) -> AsyncEngine:
    """An async engine for `url`.

    `search_path` puts tables in an isolated schema (used by tests); it needs a direct URL.
    `null_pool` opens a fresh connection per checkout, for one-off commands.
    """
    pooled = is_pooled(url)
    connect_args: dict[str, Any] = {}
    if pooled:
        connect_args["prepare_threshold"] = None
    if search_path is not None:
        if pooled:
            raise ValueError("search_path needs the direct (non-pooled) database URL")
        schema = validate_schema_name(search_path)
        connect_args["options"] = f"-csearch_path={schema},public"

    pool_args: dict[str, Any] = (
        {"poolclass": NullPool}
        if null_pool
        else {
            "pool_size": 5,
            "max_overflow": 5,
            "pool_pre_ping": True,  # Neon scales to zero and drops idle connections
            "pool_recycle": 300,
        }
    )
    return create_async_engine(sqlalchemy_url(url), connect_args=connect_args, **pool_args)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
