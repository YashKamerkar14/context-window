"""Fixtures for driving a server in memory against recorded or hand-made HTTP responses."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Client, FastMCP

from context_window_mcp.http import HttpConfig, PoliteClient
from tests.replay import ReplayTransport

ServerFactory = Callable[..., FastMCP]


async def _no_sleep(_: float) -> None:
    return None


def make_client_factory(transport: httpx.AsyncBaseTransport) -> Callable[[], PoliteClient]:
    def factory() -> PoliteClient:
        return PoliteClient(HttpConfig(), transport=transport, sleep=_no_sleep)

    return factory


@asynccontextmanager
async def replay(
    server: ServerFactory, fixture: str
) -> AsyncGenerator[tuple[Client[Any], ReplayTransport]]:
    """An in-memory client for `server`, answering HTTP from a recorded fixture."""
    transport = ReplayTransport(fixture)
    async with Client(server(make_client_factory(transport))) as client:
        yield client, transport


Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def mocked(server: ServerFactory, handler: Handler) -> AsyncGenerator[Client[Any]]:
    """An in-memory client for `server`, answering HTTP with `handler`."""
    async with Client(server(make_client_factory(httpx.MockTransport(handler)))) as client:
        yield client


async def call(client: Client[Any], tool: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert result.structured_content is not None
    return result.structured_content
