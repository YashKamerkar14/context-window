from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.config import REPO_ROOT
from context_window.db.models import Interest, Source, WatchlistRepo
from context_window.schemas.enums import Beat, SourceKind
from context_window.settings_file import (
    InterestSpec,
    RepoSpec,
    SettingsFile,
    SourceSpec,
    sync_settings,
)


def _settings(
    sources: list[SourceSpec] | None = None,
    repos: list[RepoSpec] | None = None,
    interests: list[InterestSpec] | None = None,
) -> SettingsFile:
    return SettingsFile(sources=sources or [], watchlist=repos or [], interests=interests or [])


def _source(name: str, authority: int = 3) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "name": name,
            "beat": Beat.INDUSTRY,
            "kind": SourceKind.RSS,
            "url": f"https://{name}.example/feed",
            "authority": authority,
        }
    )


async def test_loads_the_real_settings_file_and_is_idempotent(session: AsyncSession) -> None:
    settings = SettingsFile.load(REPO_ROOT / "config" / "settings.toml")
    first = await sync_settings(session, settings)
    second = await sync_settings(session, settings)
    assert first == second
    assert second.sources_disabled == second.repos_disabled == second.interests_deleted == 0

    sources = (await session.scalars(select(Source))).all()
    assert len(sources) == len(settings.sources)
    arxiv = next(s for s in sources if s.kind is SourceKind.ARXIV)
    assert arxiv.options["categories"] == ["cs.AI", "cs.CL", "cs.LG"]
    repos = (await session.scalars(select(WatchlistRepo))).all()
    assert {(r.owner, r.repo) for r in repos} >= {
        ("langchain-ai", "langgraph"),
        ("prefecthq", "fastmcp"),
    }
    assert len((await session.scalars(select(Interest))).all()) == 5


async def test_removed_sources_and_repos_are_disabled_not_deleted(session: AsyncSession) -> None:
    await sync_settings(
        session,
        _settings(
            sources=[_source("a"), _source("b")],
            repos=[RepoSpec(owner="x", repo="one"), RepoSpec(owner="x", repo="two")],
        ),
    )
    report = await sync_settings(
        session,
        _settings(sources=[_source("a", authority=5)], repos=[RepoSpec(owner="x", repo="one")]),
    )
    assert report.sources_disabled == 1
    assert report.repos_disabled == 1

    by_name = {s.name: s for s in (await session.scalars(select(Source))).all()}
    assert by_name["a"].enabled and by_name["a"].authority == 5
    assert not by_name["b"].enabled
    repos = {r.repo: r.enabled for r in (await session.scalars(select(WatchlistRepo))).all()}
    assert repos == {"one": True, "two": False}


async def test_learned_interest_weights_survive_a_reload(session: AsyncSession) -> None:
    spec = InterestSpec(topic="MCP", description="the protocol")
    await sync_settings(session, _settings(interests=[spec]))
    interest = await session.scalar(select(Interest))
    assert interest is not None
    interest.weight = 1.7  # as if feedback had nudged it
    interest.embedding_model = "embed-v1"
    await session.flush()

    await sync_settings(session, _settings(interests=[spec]))
    assert interest.weight == 1.7
    assert interest.embedding_model == "embed-v1"

    await sync_settings(session, _settings(interests=[spec]), reset_weights=True)
    assert interest.weight == 1.0


async def test_changed_description_clears_the_embedding(session: AsyncSession) -> None:
    await sync_settings(
        session, _settings(interests=[InterestSpec(topic="MCP", description="old")])
    )
    interest = await session.scalar(select(Interest))
    assert interest is not None
    interest.embedding_model = "embed-v1"
    await session.flush()

    await sync_settings(
        session, _settings(interests=[InterestSpec(topic="mcp", description="new")])
    )
    assert interest.description == "new"
    assert interest.embedding_model is None


async def test_removed_interests_are_deleted(session: AsyncSession) -> None:
    await sync_settings(
        session,
        _settings(
            interests=[
                InterestSpec(topic="A", description="a"),
                InterestSpec(topic="B", description="b"),
            ]
        ),
    )
    report = await sync_settings(
        session, _settings(interests=[InterestSpec(topic="A", description="a")])
    )
    assert report.interests_deleted == 1
    assert [i.topic for i in (await session.scalars(select(Interest))).all()] == ["A"]
