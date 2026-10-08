"""The releases beat: what changed in the tools on the watchlist.

Fetching is deterministic code; the model never chooses tools. For each watchlist repo the
desk reads GitHub Releases (filtered by the repo's tag pattern) and, when the repo has a
PyPI package, the package's release feed. A GitHub release and the PyPI release of the
same version are one lead: the GitHub page, with its notes, is the primary source
(PLAN.md 6.6) and the PyPI page a secondary one. Pre-releases are left out.
"""

import asyncio
import datetime as dt
import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import Source, WatchlistRepo
from context_window.db.queries import (
    NewItem,
    SavedItem,
    consumed_keys,
    reportable_items,
    save_items,
)
from context_window.graph.reporter.drafts import DraftStory
from context_window.mcp_client import NewsroomTools, ToolCallError
from context_window.memory.canonical import (
    github_release_key,
    normalize_pypi_name,
    pypi_release_key,
)
from context_window.schemas.enums import Beat, SourceKind
from context_window.schemas.newsroom import Claim, RunError, SentenceClaims, StoryCandidate

# Releases published within this window are fetched. It matches the reporter filter's
# 72-hour age limit: anything older could not be reported anyway.
LOOKBACK = dt.timedelta(hours=72)
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
class ReleaseSources:
    github_id: int | None
    pypi_id: int | None
    pypi_feed: str


@dataclass(frozen=True)
class FetchedRelease:
    source_id: int
    repo: str
    # The tag's own package prefix in a monorepo, otherwise the repo's PyPI package.
    package: str | None
    version: str
    url: str
    key: str
    title: str
    published_at: dt.datetime | None
    notes: str
    from_github: bool


class ReleaseLead(BaseModel):
    """One release to report, small enough for the reporter's checkpointed state. Its notes
    are in the run's fetch cache under `url`."""

    repo: str
    package: str | None
    version: str
    url: str
    source_urls: list[str]
    canonical_keys: list[str]
    published_at: dt.datetime | None
    has_notes: bool


# --- Sources ---------------------------------------------------------------------------


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
        ReleaseSources(github.id if github else None, pypi.id if pypi else None, feed),
        [Repo(r.owner, r.repo, r.pypi_package, r.tag_pattern) for r in repos],
    )


# --- Fetching --------------------------------------------------------------------------


def parse_tag(tag: str) -> tuple[str | None, str]:
    """(package prefix or None, version) for a release tag; the tag itself if unparseable."""
    match = _TAG.match(tag.strip())
    if match is None:
        return None, tag
    name = match.group("name")
    return (normalize_pypi_name(name) if name else None), match.group("version")


def _datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


async def _github(
    tools: NewsroomTools, source_id: int, repo: Repo, since: dt.datetime
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
        found.append(
            FetchedRelease(
                source_id=source_id,
                repo=repo.full_name,
                package=prefix or repo.pypi_package,
                version=version,
                url=str(release["url"]),
                key=github_release_key(repo.owner, repo.repo, tag),
                title=f"{repo.full_name} {tag}",
                published_at=_datetime(release.get("published_at")),
                notes=str(release.get("body") or ""),
                from_github=True,
            )
        )
    return found


async def _pypi(
    tools: NewsroomTools, source_id: int, feed: str, package: str, repo: Repo, since: dt.datetime
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
    for item in result.get("items", []):
        version = str(item.get("title") or "").strip()
        link = item.get("link")
        if not link or not _FINAL_VERSION.match(version):
            continue  # pre-releases, dev and post releases
        found.append(
            FetchedRelease(
                source_id=source_id,
                repo=repo.full_name,
                package=package,
                version=version,
                url=str(link),
                key=pypi_release_key(package, version),
                title=f"{package} {version} on PyPI",
                published_at=_datetime(item.get("published_at")),
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
        if sources.github_id is not None:
            calls.append(
                (
                    f"github:{repo.full_name}",
                    partial(_github, tools, sources.github_id, repo, since),
                )
            )
        if sources.pypi_id is not None and repo.pypi_package:
            fetch = partial(
                _pypi, tools, sources.pypi_id, sources.pypi_feed, repo.pypi_package, repo, since
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


# --- Grouping, saving and filtering ----------------------------------------------------


def group_releases(releases: Sequence[FetchedRelease]) -> list[list[FetchedRelease]]:
    """Group the GitHub and PyPI copies of one release; the GitHub copy comes first."""
    groups: dict[tuple[str, str | None, str], list[FetchedRelease]] = {}
    for release in releases:
        group = groups.setdefault((release.repo, release.package, release.version), [])
        if all(r.key != release.key for r in group):
            group.append(release)
    for group in groups.values():
        group.sort(key=lambda r: not r.from_github)
    return list(groups.values())


def _new_item(release: FetchedRelease) -> NewItem:
    notes = release.notes.strip()
    return NewItem(
        url=release.url,
        title=release.title,
        published_at=release.published_at,
        excerpt=notes or None,
        content_hash=hashlib.sha256(notes.encode()).hexdigest() if notes else None,
        key=release.key,
    )


async def save_releases(
    session: AsyncSession, releases: Sequence[FetchedRelease]
) -> dict[str, SavedItem]:
    """Save every fetched release as an item; return them all by canonical key."""
    by_source: dict[int, list[FetchedRelease]] = {}
    for release in releases:
        by_source.setdefault(release.source_id, []).append(release)
    saved: dict[str, SavedItem] = {}
    for source_id, batch in by_source.items():
        for item in await save_items(session, source_id, [_new_item(r) for r in batch]):
            saved[item.canonical_key] = item
    return saved


def _published(group: Sequence[FetchedRelease]) -> dt.datetime | None:
    dates = [r.published_at for r in group if r.published_at is not None]
    return min(dates) if dates else None


async def reportable_leads(
    session: AsyncSession,
    groups: Sequence[Sequence[FetchedRelease]],
    saved: dict[str, SavedItem],
    *,
    now: dt.datetime,
    max_items: int,
) -> list[ReleaseLead]:
    """The reporter filter, per lead: none of its items consumed, and its primary item
    first fetched less than 72 hours ago. Newest first, at most `max_items`."""
    primaries = [saved[g[0].key] for g in groups if g[0].key in saved]
    fresh = {item.canonical_key for item in await reportable_items(session, primaries, now=now)}
    consumed = await consumed_keys(session, (r.key for g in groups for r in g))

    leads: list[ReleaseLead] = []
    for group in groups:
        keys = [r.key for r in group]
        if keys[0] not in fresh or consumed.intersection(keys):
            continue
        primary = group[0]
        leads.append(
            ReleaseLead(
                repo=primary.repo,
                package=primary.package,
                version=primary.version,
                url=primary.url,
                source_urls=[r.url for r in group],
                canonical_keys=keys,
                published_at=_published(group),
                has_notes=bool(primary.notes.strip()),
            )
        )
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    leads.sort(key=lambda lead: lead.published_at or epoch, reverse=True)
    return leads[:max_items]


# --- Drafts to candidates --------------------------------------------------------------


def _clean(text: str) -> str:
    return " ".join(text.split())


def to_candidate(draft: DraftStory, lead: ReleaseLead) -> StoryCandidate:
    """A validated candidate. URLs and keys come from the lead, never from the model.
    Raises pydantic.ValidationError when the draft breaks the candidate's rules."""
    sentences = [
        SentenceClaims(sentence=_clean(s.text), claim_ids=[c.strip() for c in s.claim_ids])
        for s in draft.summary
    ]
    return StoryCandidate(
        beat=Beat.RELEASES,
        headline=_clean(draft.headline),
        summary=" ".join(s.sentence for s in sentences),
        why_it_matters=_clean(draft.why_it_matters),
        claims=[
            Claim(id=c.id.strip(), text=_clean(c.text), source_url=lead.url) for c in draft.claims
        ],
        sentence_claim_map=sentences,
        source_urls=lead.source_urls,
        canonical_keys=lead.canonical_keys,
        published_at=lead.published_at,
        is_breaking_change=draft.is_breaking_change,
    )
