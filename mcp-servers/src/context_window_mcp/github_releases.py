"""GitHub Releases for watchlist repos.

Run on its own: `python -m context_window_mcp.github_releases` (stdio). Reads GITHUB_TOKEN
from the environment; without it GitHub allows only 60 requests an hour.
"""

import datetime as dt
import json
import os
import re
from typing import Any, cast
from urllib.parse import quote

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from context_window_mcp.common import (
    ClientFactory,
    build_server,
    http_client,
    is_since,
    parse_iso,
    run_stdio,
    truncate,
)
from context_window_mcp.http import Fetched, FetchError, HttpConfig, PoliteClient

API = "https://api.github.com"
# About 1.5k tokens: enough for a changelog's headline changes, not a whole history.
BODY_CHARS = 6000
PAGE_SIZE = 30
MAX_PAGES = 5
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def github_client_factory() -> PoliteClient:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return PoliteClient(HttpConfig.from_env(), headers=headers)


class Release(BaseModel):
    tag: str
    name: str
    body: str = Field(description="Release notes in Markdown, at most 6000 characters")
    body_truncated: bool
    url: str
    published_at: dt.datetime | None
    prerelease: bool


class ReleasesResult(BaseModel):
    owner: str
    repo: str
    releases: list[Release] = Field(default_factory=list[Release])
    error: str | None = None


class ReleaseResult(BaseModel):
    release: Release | None = None
    error: str | None = None


def _release(raw: dict[str, Any]) -> Release:
    body = str(raw.get("body") or "")
    return Release(
        tag=str(raw["tag_name"]),
        name=str(raw.get("name") or raw["tag_name"]),
        body=truncate(body, BODY_CHARS),
        body_truncated=len(body) > BODY_CHARS,
        url=str(raw["html_url"]),
        published_at=parse_iso(cast(str | None, raw.get("published_at"))),
        prerelease=bool(raw.get("prerelease")),
    )


def _api_error(fetched: Fetched) -> str:
    if fetched.status == 404:
        return "repository or release not found"
    if fetched.status in (403, 429) and fetched.headers.get("x-ratelimit-remaining") == "0":
        reset = fetched.headers.get("x-ratelimit-reset", "?")
        return f"GitHub rate limit exhausted (resets at unix time {reset}); set GITHUB_TOKEN"
    return f"HTTP {fetched.status}"


def _valid(owner: str, repo: str) -> str | None:
    if not (_NAME.match(owner) and _NAME.match(repo)):
        return f"invalid owner or repo name: {owner}/{repo}"
    return None


def create_server(client_factory: ClientFactory = github_client_factory) -> FastMCP:
    mcp = build_server(
        "github-releases",
        "Lists GitHub releases for a repository, newest first, with truncated release notes.",
        client_factory,
    )

    @mcp.tool
    async def list_releases(
        owner: str,
        repo: str,
        ctx: Context,
        since: dt.datetime | None = None,
        tag_pattern: str | None = Field(
            default=None, description="Python regex the tag must fully match"
        ),
        include_prereleases: bool = False,
        max_items: int = Field(default=20, ge=1, le=100),
    ) -> ReleasesResult:
        """Releases published at or after `since`, newest first. Drafts are never returned."""
        if (problem := _valid(owner, repo)) is not None:
            return ReleasesResult(owner=owner, repo=repo, error=problem)
        try:
            pattern = re.compile(tag_pattern) if tag_pattern else None
        except re.error as exc:
            return ReleasesResult(owner=owner, repo=repo, error=f"invalid tag_pattern: {exc}")

        client = http_client(ctx)
        found: list[Release] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                fetched = await client.get(
                    f"{API}/repos/{owner}/{repo}/releases",
                    params={"per_page": PAGE_SIZE, "page": page},
                )
            except FetchError as exc:
                return ReleasesResult(owner=owner, repo=repo, releases=found, error=str(exc))
            if not fetched.ok:
                return ReleasesResult(
                    owner=owner, repo=repo, releases=found, error=_api_error(fetched)
                )

            raw_page = cast(list[dict[str, Any]], _json(fetched))
            reached_old = False
            for raw in raw_page:
                if raw.get("draft"):
                    continue
                release = _release(raw)
                if not is_since(release.published_at, since):
                    reached_old = True
                    continue
                if release.prerelease and not include_prereleases:
                    continue
                if pattern is not None and not pattern.fullmatch(release.tag):
                    continue
                found.append(release)
            # GitHub lists newest first, so a page reaching past `since` ends the search.
            if reached_old or len(raw_page) < PAGE_SIZE or len(found) >= max_items:
                break

        epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
        found.sort(key=lambda r: r.published_at or epoch, reverse=True)
        return ReleasesResult(owner=owner, repo=repo, releases=found[:max_items])

    @mcp.tool
    async def get_release(owner: str, repo: str, tag: str, ctx: Context) -> ReleaseResult:
        """One release by its tag."""
        if (problem := _valid(owner, repo)) is not None:
            return ReleaseResult(error=problem)
        url = f"{API}/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
        try:
            fetched = await http_client(ctx).get(url)
        except FetchError as exc:
            return ReleaseResult(error=str(exc))
        if not fetched.ok:
            return ReleaseResult(error=_api_error(fetched))
        return ReleaseResult(release=_release(cast(dict[str, Any], _json(fetched))))

    return mcp


def _json(fetched: Fetched) -> object:
    return json.loads(fetched.content)


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
