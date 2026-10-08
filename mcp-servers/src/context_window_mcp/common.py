"""Helpers shared by the servers: text cleanup, dates and the server_info tool."""

import datetime as dt
import html
import os
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Context, FastMCP
from pydantic import BaseModel

from context_window_mcp import __version__
from context_window_mcp.http import HttpConfig, PoliteClient

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

ClientFactory = Callable[[], PoliteClient]


def default_client_factory() -> PoliteClient:
    return PoliteClient(HttpConfig.from_env())


def strip_html(text: str | None) -> str:
    """Plain text from an HTML snippet: tags removed, entities decoded, spaces collapsed."""
    if not text:
        return ""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return as_utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_since(published: dt.datetime | None, since: dt.datetime | None) -> bool:
    """Keep undated entries; the caller decides what to do with them."""
    return since is None or published is None or published >= as_utc(since)


class ServerInfo(BaseModel):
    server: str
    version: str
    pid: int


def build_server(name: str, instructions: str, client_factory: ClientFactory) -> FastMCP:
    """A FastMCP server whose tools share one PoliteClient for the process lifetime."""

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncGenerator[dict[str, Any]]:
        async with client_factory() as client:
            yield {"http": client}

    mcp = FastMCP(name, instructions=instructions, version=__version__, lifespan=lifespan)

    @mcp.tool
    async def server_info() -> ServerInfo:
        """Name, version and process id of this server (used to verify session reuse)."""
        return ServerInfo(server=name, version=__version__, pid=os.getpid())

    return mcp


def run_stdio(mcp: FastMCP) -> None:
    """Serve over stdio without FastMCP's decorative banner (it would clutter run logs)."""
    mcp.run(transport="stdio", show_banner=False)


def http_client(ctx: Context) -> PoliteClient:
    client = ctx.lifespan_context["http"]
    assert isinstance(client, PoliteClient)
    return client
