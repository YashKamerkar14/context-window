"""Test doubles for the newsroom: an embedder, stub agents, in-memory MCP servers and a
context that runs nodes against the rolled-back test session."""

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from context_window_mcp.http import HttpConfig, PoliteClient
from fastmcp.client.transports import FastMCPTransport
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.embeddings import Vector
from context_window.graph.context import Agents, NewsroomContext, StoryWriter
from context_window.graph.reporter.drafts import (
    DraftClaim,
    DraftSentence,
    DraftStory,
    LeadText,
    SkippedItem,
    StoryDrafts,
)
from context_window.mcp_client import NewsroomTools
from context_window.schemas.agents import CoverageBrief, CoveragePlan, StoryPair
from context_window.schemas.enums import Beat
from context_window.schemas.newsroom import BeatAssignment, Story

EMBED_DIM = 1536  # the database's vector width
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


def settings(**values: Any) -> Settings:
    defaults: dict[str, Any] = {
        "embedding_model": "test/embed",
        "model_prices": {"test/embed": {"in": 0.02, "out": 0.0}},
    }
    return Settings(_env_file=None, **(defaults | values))  # pyright: ignore[reportCallIssue]


def embed_text(text: str) -> Vector:
    """Bag of words hashed into EMBED_DIM buckets: texts sharing words point the same way."""
    vector = [0.0] * EMBED_DIM
    for word in _WORD.findall(text.lower()):
        bucket = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % EMBED_DIM
        vector[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


async def fake_embed(texts: Sequence[str]) -> list[Vector]:
    return [embed_text(text) for text in texts]


def draft_for(lead: LeadText, headline: str | None = None) -> DraftStory:
    title = headline or lead.title
    return DraftStory(
        item_id=lead.id,
        headline=title,
        summary=[DraftSentence(text=f"{title} was announced.", claim_ids=["c1"])],
        why_it_matters="Engineers may want to look at it.",
        claims=[DraftClaim(id="c1", text=f"{title} was announced.")],
        is_breaking_change=False,
    )


class StubWriter:
    """Writes a story for every lead that has text, and records what it was given."""

    def __init__(self, skip: Callable[[LeadText], bool] = lambda lead: False) -> None:
        self.calls: list[tuple[Beat, list[LeadText]]] = []
        self.skip = skip

    async def __call__(
        self, beat: Beat, leads: Sequence[LeadText], assignment: BeatAssignment
    ) -> StoryDrafts:
        self.calls.append((beat, list(leads)))
        stories = [draft_for(lead) for lead in leads if not self.skip(lead)]
        skipped = [SkippedItem(item_id=lead.id, reason="stub") for lead in leads if self.skip(lead)]
        return StoryDrafts(stories=stories, skipped=skipped)


async def plan_nothing(brief: CoverageBrief) -> CoveragePlan:
    return CoveragePlan(assignments=[])


async def never_same(pairs: Sequence[StoryPair]) -> list[bool]:
    return [False for _ in pairs]


async def plain_intro(date: object, stories: Sequence[Story], light_day: bool) -> str:
    return f"{len(stories)} stories today."


def agents(**overrides: Any) -> Agents:
    values: dict[str, Any] = {
        "plan_coverage": plan_nothing,
        "write_stories": StubWriter(),
        "same_event": never_same,
        "write_intro": plain_intro,
    }
    return Agents(**(values | overrides))


def transport(
    create_server: Any, handler: Callable[[httpx.Request], httpx.Response]
) -> FastMCPTransport:
    """An in-memory MCP server whose HTTP requests go to `handler`."""

    def factory() -> PoliteClient:
        return PoliteClient(HttpConfig(), transport=httpx.MockTransport(handler))

    return FastMCPTransport(create_server(factory))


@asynccontextmanager
async def newsroom_context(
    session: AsyncSession,
    transports: Mapping[str, FastMCPTransport],
    *,
    writer: StoryWriter | None = None,
    now: Any = None,
    **agent_overrides: Any,
) -> AsyncGenerator[NewsroomContext]:
    # Parallel branches share the one test session, which cannot be used concurrently;
    # production opens a session per block instead.
    lock = asyncio.Lock()

    @asynccontextmanager
    async def db() -> AsyncGenerator[AsyncSession]:
        async with lock:
            yield session

    config = settings()
    overrides = dict(agent_overrides)
    if writer is not None:
        overrides["write_stories"] = writer
    clock: dict[str, Any] = {"now": now} if now is not None else {}
    async with NewsroomTools(settings=config, transports=transports) as tools:
        yield NewsroomContext(
            settings=config,
            tools=tools,
            db=db,
            embed=fake_embed,
            agents=agents(**overrides),
            **clock,
        )
