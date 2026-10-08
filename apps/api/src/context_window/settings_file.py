"""The settings file (config/settings.toml): sources, repo watchlist and interests.

The file is the source of truth. Loading it upserts every entry; sources and repos missing
from the file are disabled (items still reference them), and interests missing from it
are deleted. Interest weights learned from feedback are kept unless `reset_weights`.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import Interest, Source, WatchlistRepo
from context_window.memory.canonical import normalize_pypi_name
from context_window.schemas.enums import Beat, SourceKind

_GITHUB_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSpec(_Strict):
    name: str = Field(min_length=1, max_length=200)
    beat: Beat
    kind: SourceKind
    url: HttpUrl
    authority: int = Field(ge=1, le=5)
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class RepoSpec(_Strict):
    owner: str
    repo: str
    pypi_package: str | None = None
    tag_pattern: str | None = Field(default=None, max_length=200)
    enabled: bool = True

    @field_validator("owner", "repo")
    @classmethod
    def _github_name(cls, value: str) -> str:
        if not _GITHUB_NAME.match(value):
            raise ValueError(f"not a valid GitHub owner or repo name: {value!r}")
        return value.lower()

    @field_validator("pypi_package")
    @classmethod
    def _pypi_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_pypi_name(value)

    @field_validator("tag_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"tag_pattern is not a valid regex: {exc}") from exc
        return value


class InterestSpec(_Strict):
    topic: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    weight: float = Field(default=1.0, ge=0.2, le=2.0)


class SettingsFile(_Strict):
    sources: list[SourceSpec] = Field(default_factory=list[SourceSpec])
    watchlist: list[RepoSpec] = Field(default_factory=list[RepoSpec])
    interests: list[InterestSpec] = Field(default_factory=list[InterestSpec])

    @model_validator(mode="after")
    def _unique(self) -> Self:
        _require_unique("source url", [str(s.url) for s in self.sources])
        _require_unique("watchlist repo", [f"{r.owner}/{r.repo}" for r in self.watchlist])
        _require_unique("interest topic", [i.topic.lower() for i in self.interests])
        return self

    @classmethod
    def load(cls, path: Path) -> "SettingsFile":
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))


def _require_unique(what: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: {value}")
        seen.add(value)


@dataclass(frozen=True)
class SyncReport:
    sources: int
    sources_disabled: int
    repos: int
    repos_disabled: int
    interests: int
    interests_deleted: int


async def sync_settings(
    session: AsyncSession, settings: SettingsFile, *, reset_weights: bool = False
) -> SyncReport:
    """Apply the settings file to the database. The caller commits."""
    # Sources, matched on URL.
    source_urls = [str(s.url) for s in settings.sources]
    if settings.sources:
        stmt = insert(Source).values(
            [
                {
                    "name": s.name,
                    "beat": s.beat,
                    "kind": s.kind,
                    "url": str(s.url),
                    "authority": s.authority,
                    "enabled": s.enabled,
                    "options": s.options,
                }
                for s in settings.sources
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Source.url],
                set_={
                    key: stmt.excluded[key]
                    for key in ("name", "beat", "kind", "authority", "enabled", "options")
                },
            )
        )
    disabled_sources = cast(
        CursorResult[Any],
        await session.execute(
            update(Source)
            .where(Source.url.not_in(source_urls), Source.enabled.is_(True))
            .values(enabled=False)
        ),
    )

    # Watchlist, matched on owner/repo.
    if settings.watchlist:
        stmt = insert(WatchlistRepo).values(
            [
                r.model_dump(include={"owner", "repo", "pypi_package", "tag_pattern", "enabled"})
                for r in settings.watchlist
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[WatchlistRepo.owner, WatchlistRepo.repo],
                set_={
                    key: stmt.excluded[key] for key in ("pypi_package", "tag_pattern", "enabled")
                },
            )
        )
    keep = {(r.owner, r.repo) for r in settings.watchlist}
    existing_repos = (await session.execute(select(WatchlistRepo))).scalars().all()
    repos_disabled = 0
    for repo in existing_repos:
        if (repo.owner, repo.repo) not in keep and repo.enabled:
            repo.enabled = False
            repos_disabled += 1

    # Interests, matched on topic. A changed description invalidates the stored embedding.
    existing = {i.topic.lower(): i for i in (await session.scalars(select(Interest))).all()}
    for spec in settings.interests:
        current = existing.get(spec.topic.lower())
        if current is None:
            session.add(
                Interest(topic=spec.topic, description=spec.description, weight=spec.weight)
            )
            continue
        if current.description != spec.description:
            current.description = spec.description
            current.embedding = None
            current.embedding_model = None
        if reset_weights:
            current.weight = spec.weight
    wanted_topics = {i.topic.lower() for i in settings.interests}
    stale_ids = [i.id for key, i in existing.items() if key not in wanted_topics]
    if stale_ids:
        await session.execute(delete(Interest).where(Interest.id.in_(stale_ids)))
    await session.flush()

    return SyncReport(
        sources=len(settings.sources),
        sources_disabled=disabled_sources.rowcount,
        repos=len(settings.watchlist),
        repos_disabled=repos_disabled,
        interests=len(settings.interests),
        interests_deleted=len(stale_ids),
    )
