import json
from typing import Any

import httpx

from context_window_mcp import github_releases
from tests.conftest import call, mocked, replay

LANGGRAPH = {"owner": "langchain-ai", "repo": "langgraph"}


async def test_lists_real_releases_newest_first() -> None:
    async with replay(github_releases.create_server, "github_langgraph") as (client, _):
        result = await call(client, "list_releases", **LANGGRAPH)
    assert result["error"] is None
    tags = [r["tag"] for r in result["releases"]]
    assert tags[:4] == ["cli==0.4.33", "1.2.14", "sdk==0.4.6", "1.2.13"]
    first = result["releases"][1]
    assert first["url"] == "https://github.com/langchain-ai/langgraph/releases/tag/1.2.14"
    assert first["published_at"].startswith("2026-10-06")


async def test_tag_pattern_keeps_only_core_releases() -> None:
    async with replay(github_releases.create_server, "github_langgraph") as (client, _):
        result = await call(client, "list_releases", **LANGGRAPH, tag_pattern=r"\d+\.\d+\.\d+")
    assert [r["tag"] for r in result["releases"]] == ["1.2.14", "1.2.13", "1.2.12"]


async def test_since_stops_at_older_releases() -> None:
    async with replay(github_releases.create_server, "github_langgraph") as (client, _):
        result = await call(client, "list_releases", **LANGGRAPH, since="2026-10-05T00:00:00Z")
    assert [r["tag"] for r in result["releases"]] == [
        "cli==0.4.33",
        "1.2.14",
        "sdk==0.4.6",
        "1.2.13",
    ]


async def test_get_release() -> None:
    async with replay(github_releases.create_server, "github_get_release") as (client, _):
        result = await call(client, "get_release", **LANGGRAPH, tag="1.2.14")
    assert result["error"] is None
    assert result["release"]["tag"] == "1.2.14"


async def test_missing_repo_is_reported() -> None:
    async with replay(github_releases.create_server, "github_missing_repo") as (client, _):
        result = await call(client, "list_releases", owner="langchain-ai", repo="no-such-repo-cw")
    assert result["releases"] == [] and result["error"] == "repository or release not found"


def _raw(tag: str, day: int, **extra: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": tag,
        "body": "notes",
        "html_url": f"https://github.com/o/r/releases/tag/{tag}",
        "published_at": f"2026-09-{day:02d}T12:00:00Z",
        "prerelease": False,
        "draft": False,
        **extra,
    }


def _pages(pages: list[list[dict[str, Any]]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        body = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(200, content=json.dumps(body).encode())

    return handler


async def test_drafts_and_prereleases_are_skipped_unless_asked() -> None:
    raw = [_raw("v3", 30, draft=True), _raw("v2-rc1", 29, prerelease=True), _raw("v1", 28)]
    async with mocked(github_releases.create_server, _pages([raw])) as client:
        default = await call(client, "list_releases", owner="o", repo="r")
        with_pre = await call(
            client, "list_releases", owner="o", repo="r", include_prereleases=True
        )
    assert [r["tag"] for r in default["releases"]] == ["v1"]
    assert [r["tag"] for r in with_pre["releases"]] == ["v2-rc1", "v1"]


async def test_paginates_until_since_is_reached() -> None:
    page1 = [_raw(f"x{i}", 29, name="x") for i in range(30)]  # filtered out by the pattern
    page2 = [_raw("v1.1.0", 20), _raw("v1.0.0", 5)]
    async with mocked(github_releases.create_server, _pages([page1, page2])) as client:
        result = await call(
            client,
            "list_releases",
            owner="o",
            repo="r",
            tag_pattern=r"v[\d.]+",
            since="2026-09-10T00:00:00Z",
        )
    assert [r["tag"] for r in result["releases"]] == ["v1.1.0"]


async def test_long_release_notes_are_truncated() -> None:
    raw = [_raw("v1", 1, body="x" * 10_000)]
    async with mocked(github_releases.create_server, _pages([raw])) as client:
        result = await call(client, "list_releases", owner="o", repo="r")
    release = result["releases"][0]
    assert release["body_truncated"] is True
    assert len(release["body"]) == github_releases.BODY_CHARS


async def test_rate_limit_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1791400000"}
        )

    async with mocked(github_releases.create_server, handler) as client:
        result = await call(client, "list_releases", owner="o", repo="r")
    assert "rate limit" in result["error"] and "GITHUB_TOKEN" in result["error"]


async def test_rejects_bad_names_and_patterns_without_calling_github() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    async with mocked(github_releases.create_server, handler) as client:
        bad_name = await call(client, "list_releases", owner="../etc", repo="r")
        bad_regex = await call(client, "list_releases", owner="o", repo="r", tag_pattern="v(")
    assert bad_name["error"].startswith("invalid owner or repo")
    assert bad_regex["error"].startswith("invalid tag_pattern")


async def test_token_is_sent_when_set(monkeypatch: Any) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    client = github_releases.github_client_factory()
    try:
        headers = client._client.headers  # pyright: ignore[reportPrivateUsage]
        assert headers["authorization"] == "Bearer test-token"
        assert headers["accept"] == "application/vnd.github+json"
    finally:
        await client.aclose()
