"""What every node needs at run time but must not be checkpointed: settings, the MCP
sessions, the database and the model-backed writers.

LangGraph passes this to nodes as `runtime.context`. Tests swap the writers for stubs
and the database for a rolled-back session.
"""

import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.graph.reporter.drafts import ReleaseDrafts, ReleaseNotes
from context_window.mcp_client import NewsroomTools
from context_window.schemas.newsroom import BeatAssignment

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Turns a batch of release notes into the model's drafts.
ReleaseWriter = Callable[[Sequence[ReleaseNotes], BeatAssignment], Awaitable[ReleaseDrafts]]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class NewsroomContext:
    settings: Settings
    tools: NewsroomTools
    db: SessionFactory
    write_releases: ReleaseWriter
    now: Callable[[], dt.datetime] = field(default=_utc_now)
