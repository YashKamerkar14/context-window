"""The releases desk: what changed in the tools on the watchlist.

For each watchlist repo the desk reads GitHub Releases (filtered by the repo's tag
pattern) and, when the repo has a PyPI package, the package's release feed. A GitHub
release and the PyPI release of the same version are one lead: the GitHub page, with its
notes, is the primary source (PLAN.md 6.6) and the PyPI page a secondary one.
Pre-releases are left out.
"""

import asyncio
import datetime as dt
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import Source, WatchlistRepo
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.leads import (
    LOOKBACK,
    DeskResult,
    Fetched,
    Lead,
    reportable_leads,
    save_fetched,
)
from context_window.mcp_client import NewsroomTools, ToolCallError
from context_window.memory.canonical import (
    github_release_key,
    normalize_pypi_name,
    pypi_release_key,
)
from context_window.schemas.enums import Beat, SourceKind
from context_window.schemas.newsroom import BeatAssignment, RunError

MAX_PER_REPO = 20
PARALLEL_CALLS = 4
DEFAULT_PYPI_FEED = "https://pypi.org/rss/project/{package}/releases.xml"

# `v1.2.3`, `1.2.3`, or a monorepo tag such as `langchain-core==1.6.8`.
_TAG = re.compile(r"^(?:(?P<name>[A-Za-z0-9._-]+)==)?v?(?P<version>\d+(?:\.\d+)+)$")
_FINAL_VERSION = re.compile(r"^\d+(?:\.\d+)+$")


@dataclass(frozen=True)
class Repo:
    owner: str
    repo: str
    pypi_package: str | None
    tag_pattern: str | None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class SourceRef:
    id: int
    authority: int


@dataclass(frozen=True)
class ReleaseSources:
    github: SourceRef | None
    pypi: SourceRef | None
    pypi_feed: str


@dataclass(frozen=True)
class FetchedRelease:
    item: Fetched
    repo: str
    # The tag's own package prefix in a monorepo, otherwise the repo's PyPI package.
    package: str | None
    version: str
    notes: str
    from_github: bool


async def load_sources(session: AsyncSession) -> tuple[ReleaseSources, list[Repo]]:
    rows = (
        await session.scalars(
            select(Source).where(Source.beat == Beat.RELEASES, Source.enabled.is_(True))
        )
    ).all()
    github = next((s for s in rows if s.kind is SourceKind.GITHUB_RELEASES), None)
    pypi = next((s for s in rows if s.kind is SourceKind.PYPI_RELEASES), None)
    feed = str(pypi.options.get("feed_template", DEFAULT_PYPI_FEED)) if pypi else DEFAULT_PYPI_FEED
    repos = (
        await session.scalars(
            select(WatchlistRepo)
            .where(WatchlistRepo.enabled.is_(True))
            .order_by(WatchlistRepo.owner, WatchlistRepo.repo)
        )
    ).all()
    return (
        ReleaseSources(
            SourceRef(github.id, github.authority) if github else None,
            SourceRef(pypi.id, pypi.authority) if pypi else None,
            feed,
        ),
        [Repo(r.owner, r.repo, r.pypi_package, r.tag_pattern) for r in repos],
    )


def parse_tag(tag: str) -> tuple[str | None, str]:
    """(package prefix or None, version) for a release tag; the tag itself if unparseable."""
    match = _TAG.match(tag.strip())
    if match is None:
        return None, tag
    name = match.group("name")
    return (normalize_pypi_name(name) if name else None), match.group("version")


def parse_datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


async def _github(
    tools: NewsroomTools, source: SourceRef, repo: Repo, since: dt.datetime
) -> list[FetchedRelease]:
    result = await tools.call(
        "github_releases",
        "list_releases",
        {
            "owner": repo.owner,
            "repo": repo.repo,
            "since": since.isoformat(),
            "tag_pattern": repo.tag_pattern,
            "max_items": MAX_PER_REPO,
        },
    )
    if result.get("error"):
        raise ToolCallError(str(result["error"]))
    found: list[FetchedRelease] = []
    for release in result.get("releases", []):
        tag = str(release["tag"])
        prefix, version = parse_tag(tag)
        notes = str(release.get("body") or "")
        found.append(
            FetchedRelease(
                item=Fetched(
                    source_id=source.id,
                    authority=source.authority,
                    url=str(release["url"]),
                    key=github_release_key(repo.owner, repo.repo, tag),
                    title=f"{repo.full_name} {tag}",
                    published_at=parse_datetime(release.get("published_at")),
                    excerpt=notes,
                ),
                repo=repo.full_name,
                package=prefix or repo.pypi_package,
                version=version,
                notes=notes,
                from_github=True,
            )
        )
    return found


async def _pypi(
    tools: NewsroomTools,
    source: SourceRef,
    feed: str,
    package: str,
    repo: Repo,
    since: dt.datetime,
) -> list[FetchedRelease]:
    result = await tools.call(
        "rss",
        "fetch_feed",
        {
            "url": feed.format(package=package),
            "since": since.isoformat(),
            "max_items": MAX_PER_REPO,
        },
    )
    if result.get("error"):
        raise ToolCallError(str(result["error"]))
    found: list[FetchedRelease] = []
    for entry in result.get("items", []):
        version = str(entry.get("title") or "").strip()
        link = entry.get("link")
        if not link or not _FINAL_VERSION.match(version):
            continue  # pre-releases, dev and post releases
        found.append(
            FetchedRelease(
                item=Fetched(
                    source_id=source.id,
                    authority=source.authority,
                    url=str(link),
                    key=pypi_release_key(package, version),
                    title=f"{package} {version} on PyPI",
                    published_at=parse_datetime(entry.get("published_at")),
                ),
                repo=repo.full_name,
                package=package,
                version=version,
                notes="",
                from_github=False,
            )
        )
    return found


async def fetch_releases(
    tools: NewsroomTools, sources: ReleaseSources, repos: Sequence[Repo], *, now: dt.datetime
) -> tuple[list[FetchedRelease], list[RunError]]:
    """Every watchlist release published within LOOKBACK. A failing repo or feed is
    recorded as an error and skipped; it never stops the others."""
    since = now - LOOKBACK
    calls: list[tuple[str, Callable[[], Awaitable[list[FetchedRelease]]]]] = []
    for repo in repos:
        if sources.github is not None:
            fetch = partial(_github, tools, sources.github, repo, since)
            calls.append((f"github:{repo.full_name}", fetch))
        if sources.pypi is not None and repo.pypi_package:
            fetch = partial(
                _pypi, tools, sources.pypi, sources.pypi_feed, repo.pypi_package, repo, since
            )
            calls.append((f"pypi:{repo.pypi_package}", fetch))

    limit = asyncio.Semaphore(PARALLEL_CALLS)
    errors: list[RunError] = []

    async def attempt(
        label: str, call: Callable[[], Awaitable[list[FetchedRelease]]]
    ) -> list[FetchedRelease]:
        async with limit:
            try:
                return await call()
            except (ToolCallError, KeyError, ValueError) as exc:
                errors.append(RunError(node="reporter.fetch", source=label, message=str(exc)))
                return []

    results = await asyncio.gather(*(attempt(label, call) for label, call in calls))
    return [release for batch in results for release in batch], errors


def group_releases(releases: Sequence[FetchedRelease]) -> list[list[FetchedRelease]]:
    """Group the GitHub and PyPI copies of one release; the GitHub copy comes first."""
    groups: dict[tuple[str, str | None, str], list[FetchedRelease]] = {}
    for release in releases:
        group = groups.setdefault((release.repo, release.package, release.version), [])
        if all(r.item.key != release.item.key for r in group):
            group.append(release)
    for group in groups.values():
        group.sort(key=lambda r: not r.from_github)
    return list(groups.values())


def to_lead(group: Sequence[FetchedRelease]) -> Lead:
    primary = group[0]
    dates = [r.item.published_at for r in group if r.item.published_at is not None]
    labels = {"repo": primary.repo, "version": primary.version}
    if primary.package:
        labels["package"] = primary.package
    return Lead(
        beat=Beat.RELEASES,
        title=f"{primary.package or primary.repo} {primary.version}",
        url=primary.item.url,
        source_urls=[r.item.url for r in group],
        canonical_keys=[r.item.key for r in group],
        published_at=min(dates) if dates else None,
        authority=max(r.item.authority for r in group),
        # A release without notes (e.g. only on PyPI) has nothing to report or check.
        has_text=bool(primary.notes.strip()),
        labels=labels,
    )


async def releases_desk(
    ctx: NewsroomContext, assignment: BeatAssignment, now: dt.datetime
) -> DeskResult:
    async with ctx.db() as session:
        sources, repos = await load_sources(session)
    releases, errors = await fetch_releases(ctx.tools, sources, repos, now=now)
    groups = group_releases(releases)
    async with ctx.db() as session:
        saved = await save_fetched(session, [r.item for r in releases])
        leads = await reportable_leads(
            session, [to_lead(g) for g in groups], saved, now=now, max_items=assignment.max_items
        )
        await session.commit()
    notes = {g[0].item.url: g[0].notes for g in groups}
    return DeskResult(
        leads=leads,
        texts={lead.url: notes[lead.url] for lead in leads if lead.has_text},
        fetched=len(releases),
        errors=errors,
    )
