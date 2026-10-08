"""The releases reporter, end to end: in-memory MCP servers serve canned GitHub and PyPI
responses, a stub stands in for the model, and the database is the rolled-back test one."""

# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import datetime as dt
import email.utils
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from context_window_mcp import github_releases, rss
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import FetchCache, Item
from context_window.db.queries import create_run, mark_items_consumed
from context_window.graph.reporter.drafts import (
    DraftClaim,
    DraftSentence,
    DraftStory,
    LeadText,
    StoryDrafts,
)
from context_window.graph.reporter.graph import build_reporter_graph
from context_window.graph.reporter.leads import Fetched
from context_window.graph.reporter.releases import FetchedRelease, group_releases, parse_tag
from context_window.schemas.enums import Beat, RunStatus, RunTrigger
from context_window.schemas.newsroom import BeatAssignment
from context_window.settings_file import RepoSpec, SettingsFile, SourceSpec, sync_settings
from tests.fakes import StubWriter, newsroom_context, transport

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
    item = Fetched(
        source_id=1,
        authority=5,
        url=f"https://example.com/{key}",
        key=key,
        title=key,
        published_at=NOW,
    )
    return FetchedRelease(
        item=item,
        repo="langchain-ai/langchain",
        package=package,
        version=version,
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
    assert [[r.item.key for r in g] for g in groups] == [
        ["gh:langchain-core==1.6.8", "pypi:langchain-core@1.6.8"],
        ["gh:langchain==1.6.8"],
    ]


# --- End to end ------------------------------------------------------------------------


def github_handler(request: httpx.Request) -> httpx.Response:
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


def pypi_handler(request: httpx.Request) -> httpx.Response:
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
    feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
    return httpx.Response(200, text=feed)


async def load_release_settings(session: AsyncSession) -> None:
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


async def _report(session: AsyncSession, writer: Any) -> dict[str, Any]:
    run = await create_run(session, run_date=NOW.date(), trigger=RunTrigger.MANUAL)
    transports = {
        "github_releases": transport(github_releases.create_server, github_handler),
        "rss": transport(rss.create_server, pypi_handler),
    }
    async with newsroom_context(session, transports, writer=writer, now=lambda: NOW) as ctx:
        assignment = BeatAssignment(beat=Beat.RELEASES, focus="watchlist releases", max_items=40)
        output = await build_reporter_graph().ainvoke(
            {"run_id": str(run.id), "assignment": assignment}, context=ctx
        )
    run.status = RunStatus.REJECTED  # end this run so the next may start for the same date
    await session.flush()
    return {"run": run, **output}


def _not_1_2_14(lead: LeadText) -> bool:
    return lead.labels.get("version") != "1.2.14"


async def test_reports_new_releases_and_skips_consumed_ones(session: AsyncSession) -> None:
    await load_release_settings(session)
    writer = StubWriter(skip=_not_1_2_14)
    first = await _report(session, writer)

    # Only releases with notes reach the model; 1.2.12 exists only on PyPI.
    [(beat, leads)] = writer.calls
    assert beat is Beat.RELEASES
    assert [lead.labels["version"] for lead in leads] == ["1.2.14", "1.2.13"]
    assert "Durable retries" in leads[0].text

    [story] = first["candidates"]
    assert story.headline == "langgraph 1.2.14"
    assert story.claims[0].source_url == f"{GH}/1.2.14"
    assert story.source_urls == [f"{GH}/1.2.14", "https://pypi.org/project/langgraph/1.2.14/"]
    assert story.canonical_keys == ["gh:langchain-ai/langgraph@1.2.14", "pypi:langgraph@1.2.14"]

    stats = first["stats"]
    assert stats.items_fetched_by_beat == {Beat.RELEASES: 4}  # 2 GitHub + 2 final PyPI
    assert stats.candidates == 1 and stats.errors == []
    assert set(stats.timings_ms) == {"reporter.releases.fetch", "reporter.releases.write"}

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
        session, [i.id for i in items if i.canonical_key in story.canonical_keys], at=NOW
    )
    second_writer = StubWriter(skip=_not_1_2_14)
    second = await _report(session, second_writer)
    assert [[lead.labels["version"] for lead in leads] for _, leads in second_writer.calls] == [
        ["1.2.13"]
    ]
    assert second["candidates"] == []


async def test_invalid_drafts_and_writer_failures_become_errors(session: AsyncSession) -> None:
    await load_release_settings(session)

    async def broken(
        beat: Beat, leads: Sequence[LeadText], assignment: BeatAssignment
    ) -> StoryDrafts:
        # An uncited claim is repaired away; five cited claims are too many.
        repaired = DraftStory(
            item_id=leads[0].id,
            headline="x",
            summary=[DraftSentence(text="Says c1.", claim_ids=["c1"])],
            why_it_matters="y",
            claims=[DraftClaim(id="c1", text="a"), DraftClaim(id="c2", text="uncited")],
            is_breaking_change=False,
        )
        ids = [f"c{n}" for n in range(1, 6)]
        too_many = DraftStory(
            item_id=leads[1].id,
            headline="z",
            summary=[DraftSentence(text="Says five things.", claim_ids=ids)],
            why_it_matters="y",
            claims=[DraftClaim(id=i, text=i) for i in ids],
            is_breaking_change=False,
        )
        unknown = repaired.model_copy(update={"item_id": 99})
        return StoryDrafts(stories=[repaired, too_many, unknown], skipped=[])

    output = await _report(session, broken)
    [story] = output["candidates"]
    assert [c.id for c in story.claims] == ["c1"]
    messages = [e.message for e in output["stats"].errors]
    assert any("at most 4" in m for m in messages)
    assert any("unknown or repeated item 99" in m for m in messages)

    async def failing(
        beat: Beat, leads: Sequence[LeadText], assignment: BeatAssignment
    ) -> StoryDrafts:
        raise TimeoutError("model timed out")

    output = await _report(session, failing)
    assert output["candidates"] == []
    assert [e.message for e in output["stats"].errors] == ["TimeoutError: model timed out"]
