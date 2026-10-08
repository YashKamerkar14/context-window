"""MCP sessions for one newsroom run.

Each server is a separate stdio process (`python -m context_window_mcp.<name>`). A server
starts the first time one of its tools is called and stays up until the run ends, so a
run pays the start-up cost once per server rather than once per call, and the server's
in-process cache lasts exactly one run.

Servers inherit only a minimal environment plus the variables they need: the GitHub token
goes to the GitHub server alone.

Uses FastMCP's client. The plan named langchain-mcp-adapters, but no release of it works
with MCP 2, which FastMCP 4 requires (0.3.1 fails to import; 0.3.2 pins mcp<2).
"""

import asyncio
import sys
from collections.abc import Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from fastmcp import Client
from fastmcp.client.transports import ClientTransport, StdioTransport

from context_window.config import Settings, get_settings


@dataclass(frozen=True)
class ServerSpec:
    module: str
    # Settings attributes forwarded to this server's environment, as ENV_NAME -> attribute.
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])


SERVERS: dict[str, ServerSpec] = {
    "rss": ServerSpec("context_window_mcp.rss"),
    "github_releases": ServerSpec(
        "context_window_mcp.github_releases", secrets={"GITHUB_TOKEN": "github_token"}
    ),
    "arxiv": ServerSpec("context_window_mcp.arxiv"),
    "hf_papers": ServerSpec("context_window_mcp.hf_papers"),
    "hackernews": ServerSpec("context_window_mcp.hackernews"),
    "web_fetch": ServerSpec("context_window_mcp.web_fetch"),
}


class ToolCallError(RuntimeError):
    """A tool rejected its arguments or crashed. Upstream failures are not errors: tools
    report those in their result's `error` or `status` field."""


def server_env(name: str, settings: Settings) -> dict[str, str]:
    """The extra environment for one server process."""
    env = {"FASTMCP_LOG_LEVEL": "WARNING", "MAX_FETCH_BYTES": str(settings.max_fetch_bytes)}
    for variable, attribute in SERVERS[name].secrets.items():
        value = getattr(settings, attribute)
        if value is not None:
            env[variable] = value.get_secret_value()
    return env


def stdio_transport(name: str, settings: Settings) -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=["-m", SERVERS[name].module],
        env=server_env(name, settings),
        keep_alive=False,
    )


class NewsroomTools:
    """Persistent sessions to the MCP servers for the duration of one run.

    Use as `async with NewsroomTools() as tools: await tools.call("rss", "fetch_feed", ...)`.
    `transports` replaces the stdio processes, e.g. with in-memory servers in tests.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transports: Mapping[str, ClientTransport] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transports = dict(transports or {})
        self._clients: dict[str, Client[Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack = self._stack, None
        self._clients.clear()
        if stack is not None:
            await stack.aclose()

    @property
    def started(self) -> list[str]:
        return sorted(self._clients)

    async def _client(self, server: str) -> Client[Any]:
        if server not in SERVERS:
            raise KeyError(f"unknown MCP server {server!r}; known: {', '.join(SERVERS)}")
        if self._stack is None:
            raise RuntimeError("use NewsroomTools as an async context manager")
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:  # parallel first calls must not start two processes
            if server not in self._clients:
                transport = self._transports.get(server) or stdio_transport(server, self._settings)
                self._clients[server] = await self._stack.enter_async_context(Client(transport))
        return self._clients[server]

    async def call(
        self, server: str, tool: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a tool and return its structured result."""
        client = await self._client(server)
        result = await client.call_tool(tool, dict(arguments or {}), raise_on_error=False)
        if result.is_error:
            detail = " ".join(getattr(block, "text", "") for block in result.content).strip()
            raise ToolCallError(f"{server}.{tool} failed: {detail or 'no detail'}")
        if result.structured_content is None:
            raise ToolCallError(f"{server}.{tool} returned no structured content")
        return result.structured_content

    async def list_tools(self, server: str) -> list[str]:
        client = await self._client(server)
        return sorted(tool.name for tool in await client.list_tools())


def known_servers() -> Iterable[str]:
    return SERVERS.keys()
