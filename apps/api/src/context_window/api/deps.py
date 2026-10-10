"""What every endpoint shares: settings, a database session, and the admin check.

The API runs as a serverless function, so it uses the pooled DATABASE_URL (Neon's
PgBouncer) with no pool of its own: each request opens a connection through the pooler.
"""

import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from context_window.config import Settings, get_settings
from context_window.db.session import make_engine, make_sessionmaker


def settings_dep() -> Settings:
    return get_settings()


@lru_cache
def sessions() -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(make_engine(get_settings().require_pooled_url(), null_pool=True))


async def session_dep() -> AsyncGenerator[AsyncSession]:
    async with sessions()() as session:
        yield session


def session_factory_dep() -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """For endpoints that must survive a missing or broken database (health)."""
    return sessions()


SettingsDep = Annotated[Settings, Depends(settings_dep)]
SessionFactoryDep = Annotated[
    Callable[[], AbstractAsyncContextManager[AsyncSession]], Depends(session_factory_dep)
]
SessionDep = Annotated[AsyncSession, Depends(session_dep)]


def admin_dep(settings: SettingsDep, authorization: Annotated[str | None, Header()] = None) -> bool:
    """True when the request carries `Authorization: Bearer <ADMIN_TOKEN>`."""
    if settings.admin_token is None or not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    expected = settings.admin_token.get_secret_value()
    return scheme.lower() == "bearer" and secrets.compare_digest(token.strip(), expected)


IsAdmin = Annotated[bool, Depends(admin_dep)]


def require_admin(is_admin: IsAdmin) -> None:
    if not is_admin:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "admin token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


AdminOnly = Depends(require_admin)
