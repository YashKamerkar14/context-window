"""What every node needs at run time but must not be checkpointed: settings, the MCP
sessions, the database, embeddings and the model-backed agents.

LangGraph passes this to nodes as `runtime.context`. Tests swap the agents for stubs and
the database for a rolled-back session.
"""

import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.embeddings import Embedder
from context_window.graph.reporter.drafts import LeadText, StoryDrafts
from context_window.mcp_client import NewsroomTools
from context_window.schemas.agents import CoverageBrief, CoveragePlan, StoryPair
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import BeatAssignment, Story

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# A reporter's writer: a batch of leads on one beat becomes drafts.
StoryWriter = Callable[[Beat, Sequence[LeadText], BeatAssignment], Awaitable[StoryDrafts]]
# The editor-in-chief: what each reporter should look for today.
CoveragePlanner = Callable[[CoverageBrief], Awaitable[CoveragePlan]]
# For borderline pairs: is each pair about the same event? One answer per pair, in order.
SameEventJudge = Callable[[Sequence[StoryPair]], Awaitable[list[bool]]]
# The briefing's one-sentence intro.
IntroWriter = Callable[[dt.date, Sequence[Story], bool], Awaitable[str]]


@dataclass(frozen=True)
class Agents:
    plan_coverage: CoveragePlanner
    write_stories: StoryWriter
    same_event: SameEventJudge
    write_intro: IntroWriter


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class NewsroomContext:
    settings: Settings
    tools: NewsroomTools
    db: SessionFactory
    embed: Embedder
    agents: Agents
    now: Callable[[], dt.datetime] = field(default=_utc_now)
