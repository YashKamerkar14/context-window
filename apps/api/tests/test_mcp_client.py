import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from context_window_mcp import rss
from context_window_mcp.http import HttpConfig, PoliteClient
from fastmcp.client.transports import FastMCPTransport
from pydantic import SecretStr

from context_window.config import Settings
from context_window.mcp_client import SERVERS, NewsroomTools, ToolCallError, server_env


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def _in_memory_rss(handler: Callable[[httpx.Request], httpx.Response]) -> FastMCPTransport:
    def factory() -> PoliteClient:
        return PoliteClient(HttpConfig(), transport=httpx.MockTransport(handler))

    return FastMCPTransport(rss.create_server(factory))


async def test_each_server_is_one_process_reused_for_the_whole_run() -> None:
    """Real stdio processes: two calls reach the same process; servers do not share one."""
    async with NewsroomTools(settings=_settings()) as tools:
        assert tools.started == []  # nothing starts until it is needed
        first = await tools.call("rss", "server_info")
        second = await tools.call("rss", "server_info")
        other = await tools.call("web_fetch", "server_info")
        assert tools.started == ["rss", "web_fetch"]
    assert first == second
    assert first["server"] == "rss" and other["server"] == "web-fetch"
    assert first["pid"] != other["pid"]
    assert os.getpid() not in (first["pid"], other["pid"])


async def test_calls_tools_on_injected_servers() -> None:
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        "<item><title>Hello</title><link>https://e.example/1</link></item></channel></rss>"
    )
    transport = _in_memory_rss(lambda r: httpx.Response(200, text=feed))
    async with NewsroomTools(settings=_settings(), transports={"rss": transport}) as tools:
        result = await tools.call("rss", "fetch_feed", {"url": "https://e.example/feed"})
    assert [item["title"] for item in result["items"]] == ["Hello"]


async def test_invalid_arguments_raise_tool_call_error() -> None:
    transport = _in_memory_rss(lambda r: httpx.Response(200))
    async with NewsroomTools(settings=_settings(), transports={"rss": transport}) as tools:
        with pytest.raises(ToolCallError, match=r"rss\.fetch_feed failed"):
            await tools.call("rss", "fetch_feed", {"url": "https://e.example/feed", "max_items": 0})


async def test_unknown_server_and_use_outside_context() -> None:
    async with NewsroomTools(settings=_settings()) as tools:
        with pytest.raises(KeyError, match="unknown MCP server"):
            await tools.call("twitter", "search")
    with pytest.raises(RuntimeError, match="async context manager"):
        await NewsroomTools(settings=_settings()).call("rss", "server_info")


def test_github_token_goes_only_to_the_github_server() -> None:
    settings = _settings(github_token=SecretStr("gh-secret"), max_fetch_bytes=123_456)
    for name in SERVERS:
        env = server_env(name, settings)
        assert env["FASTMCP_LOG_LEVEL"] == "WARNING"
        assert env["MAX_FETCH_BYTES"] == "123456"
        assert ("GITHUB_TOKEN" in env) is (name == "github_releases")
    assert server_env("github_releases", settings)["GITHUB_TOKEN"] == "gh-secret"


def test_no_token_means_no_variable() -> None:
    assert "GITHUB_TOKEN" not in server_env("github_releases", _settings())
