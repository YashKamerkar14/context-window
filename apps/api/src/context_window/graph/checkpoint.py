"""The LangGraph checkpointer: Postgres through a small connection pool.

It uses the direct (non-pooled) URL, as LangGraph requires: `autocommit`, `dict_row`, and
no server-side prepared statements. Neon scales to zero and drops idle connections, so
the pool checks each connection before handing it out.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool


async def _check(conn: AsyncConnection[DictRow]) -> None:
    """Fail fast on a connection the server has dropped; the pool then replaces it."""
    await conn.execute("SELECT 1")


@asynccontextmanager
async def postgres_checkpointer(
    url: str, *, max_size: int = 4
) -> AsyncGenerator[AsyncPostgresSaver]:
    pool = AsyncConnectionPool[AsyncConnection[DictRow]](
        conninfo=url,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
        check=_check,
        open=False,
    )
    async with pool:
        yield AsyncPostgresSaver(pool)
