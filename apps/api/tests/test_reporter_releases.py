"""The releases reporter, end to end: in-memory MCP servers serve canned GitHub and PyPI
responses, a stub stands in for the model, and the database is the rolled-back test one."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import email.utils
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from context_window_mcp import github_releases, rss
from context_window_mcp.http import HttpConfig, PoliteClient
from fastmcp.client.transports import FastMCPTransport
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import Settings
from context_window.db.models import FetchCache, Item
from context_window.db.queries import create_run, mark_items_consumed
from context_window.graph.context import NewsroomContext
from context_window.graph.newsroom import build_newsroom_graph
from context_window.graph.reporter.drafts import (
    DraftClaim,
    DraftSentence,
    DraftStory,
    ReleaseDrafts,
    ReleaseNotes,
    SkippedRelease,
)
from context_window.graph.reporter.releases import FetchedRelease, group_releases, parse_tag
from context_window.mcp_client import NewsroomTools
from context_window.schemas.enums import Beat, RunTrigger
from context_window.schemas.newsroom import BeatAssignment
from context_window.settings_file import RepoSpec, SettingsFile, SourceSpec, sync_settings

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
GH = "https://github.com/langchain-ai/langgraph/releases/tag"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("1.2.14", (None, "1.2.14")),
        ("v0.14.4", (None, "0.14.4")),
        ("langchain-core==1.6.8", ("langchain-core", "1.6.8")),
        ("langchain_core==1.6.8", ("langchain-core", "1.6.8")),
        ("nightly", (None, "nightly")),
    ],
)
def test_parse_tag(tag: str, expected: tuple[str | None, str]) -> None:
    assert parse_tag(tag) == expected


def _fetched(key: str, package: str | None, version: str, *, github: bool) -> FetchedRelease:
    return FetchedRelease(
        source_id=1,
        repo="langchain-ai/langchain",
        package=package,
        version=version,
        url=f"https://example.com/{key}",
        key=key,
        title=key,
        published_at=NOW,
        notes="notes" if github else "",
        from_github=github,
    )


def test_group_joins_github_and_pypi_copies_of_one_package_version() -> None:
    groups = group_releases(
        [
            _fetched("pypi:langchain-core@1.6.8", "langchain-core", "1.6.8", github=False),
            _fetched("gh:langchain-core==1.6.8", "langchain-core", "1.6.8", github=True),
            _fetched("gh:langchain==1.6.8", "langchain", "1.6.8", github=True),
        ]
    )
    assert [[r.key for r in g] for g in groups] == [
        ["gh:langchain-core==1.6.8", "pypi:langchain-core@1.6.8"],
        ["gh:langchain==1.6.8"],
    ]


# --- End to end ------------------------------------------------------------------------


def _github_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/repos/langchain-ai/langgraph/releases"
    releases = [
        {
            "tag_name": tag,
            "name": tag,
            "body": body,
            "html_url": f"{GH}/{tag}",
            "published_at": (NOW - age).isoformat().replace("+00:00", "Z"),
            "prerelease": False,
            "draft": False,
        }
        for tag, body, age in [
            ("1.2.14", "## Features\n- Durable retries for tasks", dt.timedelta(hours=2)),
            ("cli==0.4.33", "CLI fixes", dt.timedelta(hours=3)),  # filtered by tag_pattern
            ("1.2.13", "## Fixes\n- Bumped dependencies", dt.timedelta(hours=30)),
            ("1.2.10", "Old", dt.timedelta(days=10)),  # older than the lookback
        ]
    ]
    return httpx.Response(200, content=json.dumps(releases))


def _pypi_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/rss/project/langgraph/releases.xml"
    items = "".join(
        f"<item><title>{version}</title>"
        f"<link>https://pypi.org/project/langgraph/{version}/</link>"
        f"<pubDate>{email.utils.format_datetime(NOW - age)}</pubDate></item>"
        for version, age in [
            ("1.2.14", dt.timedelta(hours=2)),
            ("1.3.0a1", dt.timedelta(hours=1)),  # pre-release
            ("1.2.12", dt.timedelta(hours=50)),  # on PyPI only, no notes
        ]
    )
    return httpx.Response(
        200, text=f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
    )


def _transport(create_server: Any, handler: Any) -> FastMCPTransport:
    def factory() -> PoliteClient:
        return PoliteClient(HttpConfig(), transport=httpx.MockTransport(handler))

    return FastMCPTransport(create_server(factory))


class StubWriter:
    """Writes a valid story for 1.2.14, an invalid one for anything else with features,
    and skips the rest."""

    def __init__(self) -> None:
        self.batches: list[list[ReleaseNotes]] = []

    async def __call__(
        self, releases: Sequence[ReleaseNotes], assignment: BeatAssignment
    ) -> ReleaseDrafts:
        self.batches.append(list(releases))
        stories: list[DraftStory] = []
        skipped: list[SkippedRelease] = []
        for release in releases:
            if release.version == "1.2.14":
                stories.append(
                    DraftStory(
                        release_id=release.id,
                        headline="LangGraph 1.2.14 adds durable retries",
                        summary=[
                            DraftSentence(
                                text="LangGraph 1.2.14  adds durable retries for tasks.",
                                claim_ids=["c1"],
                            )
                        ],
                        why_it_matters="Long-running agents can retry failed tasks safely.",
                        claims=[DraftClaim(id="c1", text="1.2.14 adds durable retries.")],
                        is_breaking_change=False,
                    )
                )
            else:
                skipped.append(SkippedRelease(release_id=release.id, reason="dependency bumps"))
        return ReleaseDrafts(stories=stories, skipped=skipped)


async def _load_settings(session: AsyncSession) -> None:
    await sync_settings(
        session,
        SettingsFile(
            sources=[
                SourceSpec(
                    name="GitHub Releases",
                    beat=Beat.RELEASES,
                    kind="github_releases",  # pyright: ignore[reportArgumentType]
                    url="https://api.github.com/",  # pyright: ignore[reportArgumentType]
                    authority=5,
                ),
                SourceSpec(
                    name="PyPI releases",
                    beat=Beat.RELEASES,
                    kind="pypi_releases",  # pyright: ignore[reportArgumentType]
                    url="https://pypi.org/rss/project/",  # pyright: ignore[reportArgumentType]
                    authority=4,
                ),
            ],
            watchlist=[
                RepoSpec(
                    owner="langchain-ai",
                    repo="langgraph",
                    pypi_package="langgraph",
                    tag_pattern=r"\d+\.\d+\.\d+",
                )
            ],
        ),
    )


async def _run(session: AsyncSession, writer: StubWriter) -> dict[str, Any]:
    run = await create_run(session, run_date=NOW.date(), trigger=RunTrigger.MANUAL)

    @asynccontextmanager
    async def db() -> AsyncGenerator[AsyncSession]:
        yield session

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    transports = {
        "github_releases": _transport(github_releases.create_server, _github_handler),
        "rss": _transport(rss.create_server, _pypi_handler),
    }
    async with NewsroomTools(settings=settings, transports=transports) as tools:
        context = NewsroomContext(
            settings=settings, tools=tools, db=db, write_releases=writer, now=lambda: NOW
        )
        graph = build_newsroom_graph().compile(checkpointer=InMemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": str(run.id)}}
        assignment = BeatAssignment(beat=Beat.RELEASES, focus="watchlist releases", max_items=40)
        state = await graph.ainvoke(
            {"run_id": str(run.id), "run_date": run.run_date, "assignments": [assignment]},
            config,
            context=context,
        )
    # End this run so the next one may start for the same date.
    run.status = "rejected"  # pyright: ignore[reportAttributeAccessIssue]
    await session.flush()
    return {"run": run, **state}


async def test_reports_new_releases_and_skips_consumed_ones(session: AsyncSession) -> None:
    await _load_settings(session)
    writer = StubWriter()
    first = await _run(session, writer)

    # Only releases with notes reach the model; 1.2.12 exists only on PyPI.
    assert [[r.version for r in batch] for batch in writer.batches] == [["1.2.14", "1.2.13"]]
    assert "Durable retries" in writer.batches[0][0].notes

    [story] = first["candidates"]
    assert story.headline == "LangGraph 1.2.14 adds durable retries"
    assert story.summary == "LangGraph 1.2.14 adds durable retries for tasks."
    assert story.claims[0].source_url == f"{GH}/1.2.14"
    assert story.source_urls == [f"{GH}/1.2.14", "https://pypi.org/project/langgraph/1.2.14/"]
    assert story.canonical_keys == ["gh:langchain-ai/langgraph@1.2.14", "pypi:langgraph@1.2.14"]

    stats = first["stats"]
    assert stats.items_fetched_by_beat == {Beat.RELEASES: 4}  # 2 GitHub + 2 final PyPI
    assert stats.candidates == 1 and stats.errors == []
    assert set(stats.timings_ms) == {"reporter.fetch", "reporter.write"}

    items = (await session.scalars(select(Item).order_by(Item.canonical_key))).all()
    assert [i.canonical_key for i in items] == [
        "gh:langchain-ai/langgraph@1.2.13",
        "gh:langchain-ai/langgraph@1.2.14",
        "pypi:langgraph@1.2.12",
        "pypi:langgraph@1.2.14",
    ]
    cached = await session.scalars(
        select(FetchCache.url).where(FetchCache.run_id == first["run"].id)
    )
    assert sorted(cached) == [f"feed:{GH}/1.2.13", f"feed:{GH}/1.2.14"]

    # Publishing (later phases) consumes the story's items; a second run skips them.
    await mark_items_consumed(
        session,
        [i.id for i in items if i.canonical_key in story.canonical_keys],
        at=NOW,
    )
    second_writer = StubWriter()
    second = await _run(session, second_writer)
    assert [[r.version for r in batch] for batch in second_writer.batches] == [["1.2.13"]]
    assert second["candidates"] == []


async def test_invalid_drafts_and_writer_failures_become_errors(session: AsyncSession) -> None:
    await _load_settings(session)

    async def broken(releases: Sequence[ReleaseNotes], assignment: BeatAssignment) -> ReleaseDrafts:
        story = DraftStory(
            release_id=releases[0].id,
            headline="x",
            summary=[DraftSentence(text="Says c1.", claim_ids=["c1"])],
            why_it_matters="y",
            claims=[DraftClaim(id="c1", text="a"), DraftClaim(id="c2", text="uncited")],
            is_breaking_change=False,
        )
        return ReleaseDrafts(
            stories=[story, story.model_copy(update={"release_id": 99})], skipped=[]
        )

    state = await _run(session, broken)  # pyright: ignore[reportArgumentType]
    assert state["candidates"] == []
    messages = [e.message for e in state["stats"].errors]
    assert any("not cited" in m for m in messages)
    assert any("unknown or repeated release 99" in m for m in messages)

    async def failing(
        releases: Sequence[ReleaseNotes], assignment: BeatAssignment
    ) -> ReleaseDrafts:
        raise TimeoutError("model timed out")

    state = await _run(session, failing)  # pyright: ignore[reportArgumentType]
    assert state["candidates"] == []
    assert [e.message for e in state["stats"].errors] == ["TimeoutError: model timed out"]
