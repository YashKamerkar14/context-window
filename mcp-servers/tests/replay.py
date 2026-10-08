"""Record real HTTP exchanges and replay them in tests.

A fixture is a directory with `index.json` (one entry per request) and one body file per
entry. Bodies are stored decoded; an entry recorded with `Content-Encoding: gzip` is
re-compressed on replay so the client's decoding path is exercised too.
"""

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
KEPT_HEADERS = ("content-type", "content-encoding", "retry-after", "location")


def request_key(url: str) -> str:
    """URL with its query parameters sorted, so parameter order never matters."""
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(parts._replace(query=query, fragment=""))


@dataclass(frozen=True)
class Exchange:
    url: str
    status: int
    headers: dict[str, str]
    body_file: str
    gzip: bool


def load_index(name: str) -> tuple[dict[str, Any], list[Exchange]]:
    data = cast(dict[str, Any], json.loads((FIXTURES / name / "index.json").read_text("utf-8")))
    exchanges = [Exchange(**e) for e in cast(list[dict[str, Any]], data["exchanges"])]
    return cast(dict[str, Any], data["meta"]), exchanges


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded responses; any request that was not recorded fails the test."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.meta, exchanges = load_index(name)
        self._by_key = {request_key(e.url): e for e in exchanges}
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(str(request.url))
        self.requests.append(key)
        exchange = self._by_key.get(key)
        if exchange is None:
            raise AssertionError(f"fixture {self.name!r} has no recording for {key}")
        body = (FIXTURES / self.name / exchange.body_file).read_bytes()
        headers = dict(exchange.headers)
        if exchange.gzip:
            body = gzip.compress(body)
            headers["content-encoding"] = "gzip"
        return httpx.Response(exchange.status, headers=headers, content=body, request=request)
