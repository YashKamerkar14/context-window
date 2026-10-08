"""Fetch a web page and return its main text, for fact-checking.

Run on its own: `python -m context_window_mcp.web_fetch` (stdio).

- Obeys robots.txt (RFC 9309): a 4xx robots file allows everything; a 5xx or an
  unreachable one blocks, to be safe.
- Removes scripts, styles, comments and hidden elements before extraction, so text a
  reader cannot see (a common prompt-injection trick) never reaches the fact-checker.
- Extracts the main content with trafilatura, dropping navigation, ads and footers.
- Never raises: every outcome is a status.
"""

import datetime as dt
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import trafilatura
from fastmcp import Context, FastMCP
from lxml import html as lxml_html
from pydantic import BaseModel, Field

from context_window_mcp.common import (
    ClientFactory,
    build_server,
    default_client_factory,
    http_client,
    run_stdio,
)
from context_window_mcp.http import FetchError, PoliteClient

ROBOTS_AGENT = "ContextWindow"
DEFAULT_MAX_CHARS = 32_000  # roughly 8k tokens
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain", "text/markdown"})
# lxml ships without type information, so its values are handled as Any in this module only.
_lxml: Any = lxml_html
_LXML_ERROR: type[Exception] = _lxml.etree.LxmlError
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)

Status = Literal[
    "ok", "blocked", "not_found", "http_error", "unsupported_type", "empty", "invalid_url", "error"
]


class PageResult(BaseModel):
    url: str
    final_url: str | None = None
    status: Status
    http_status: int | None = None
    title: str | None = None
    text: str = Field(default="", description="Main text of the page, plain")
    text_truncated: bool = False
    body_truncated: bool = Field(default=False, description="The raw page hit the size limit")
    content_type: str | None = None
    fetched_at: dt.datetime
    error: str | None = None


class RobotsCache:
    """robots.txt rules per origin, fetched once per process (one newsroom run)."""

    def __init__(self) -> None:
        self._rules: dict[str, RobotFileParser | bool] = {}

    async def allowed(self, client: PoliteClient, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._rules:
            self._rules[origin] = await self._load(client, origin)
        rules = self._rules[origin]
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(ROBOTS_AGENT, url)

    @staticmethod
    async def _load(client: PoliteClient, origin: str) -> RobotFileParser | bool:
        try:
            fetched = await client.get(f"{origin}/robots.txt", max_bytes=500_000)
        except FetchError:
            return False
        if 400 <= fetched.status < 500:
            return True
        if not fetched.ok:
            return False
        parser = RobotFileParser()
        parser.parse(fetched.text().splitlines())
        return parser


def clean_html(markup: bytes | str) -> str:
    """Remove everything a reader would not see. Bytes let lxml honour the page's charset."""
    try:
        tree: Any = _lxml.fromstring(markup)
    except (_LXML_ERROR, ValueError):
        return markup.decode("utf-8", "replace") if isinstance(markup, bytes) else markup
    for node in tree.xpath("//script|//style|//noscript|//template|//iframe|//comment()"):
        _drop(node)
    for node in tree.xpath('//*[@hidden or @aria-hidden="true" or @style]'):
        style = str(node.get("style") or "")
        if (
            node.get("hidden") is not None
            or node.get("aria-hidden") == "true"
            or _HIDDEN_STYLE.search(style)
        ):
            _drop(node)
    return str(_lxml.tostring(tree, encoding="unicode"))


def _drop(node: Any) -> None:
    parent = node.getparent()
    if parent is None:
        return
    # Keep the text that follows the removed element.
    if node.tail:
        previous = node.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


def extract_main_text(markup: bytes | str, url: str) -> tuple[str | None, str]:
    cleaned = clean_html(markup)
    text = trafilatura.extract(
        cleaned, url=url, include_comments=False, include_tables=True, favor_precision=True
    )
    metadata = trafilatura.extract_metadata(cleaned, default_url=url)
    title: str | None = getattr(metadata, "title", None)
    return title, (text or "").strip()


def create_server(client_factory: ClientFactory = default_client_factory) -> FastMCP:
    mcp = build_server(
        "web-fetch",
        "Fetches a web page (obeying robots.txt) and returns its main text for fact-checking.",
        client_factory,
    )
    robots = RobotsCache()

    @mcp.tool
    async def fetch_page(
        url: str,
        ctx: Context,
        max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=1000, le=200_000),
    ) -> PageResult:
        """Main text, title and final URL of a page. Never raises; see `status`."""
        now = dt.datetime.now(dt.UTC)
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return PageResult(
                url=url, status="invalid_url", fetched_at=now, error="needs an http(s) URL"
            )
        clean_url = urlunsplit(parts._replace(fragment=""))
        client = http_client(ctx)

        if not await robots.allowed(client, clean_url):
            return PageResult(
                url=url, status="blocked", fetched_at=now, error="disallowed by robots.txt"
            )
        try:
            fetched = await client.get(clean_url)
        except FetchError as exc:
            return PageResult(url=url, status="error", fetched_at=now, error=str(exc))

        def result(status: Status, **extra: Any) -> PageResult:
            return PageResult(
                url=url,
                final_url=fetched.url,
                status=status,
                http_status=fetched.status,
                content_type=fetched.content_type or None,
                body_truncated=fetched.truncated,
                fetched_at=now,
                **extra,
            )

        if fetched.status in (404, 410):
            return result("not_found")
        if not fetched.ok:
            return result("http_error", error=f"HTTP {fetched.status}")
        # A redirect may land somewhere robots.txt forbids.
        if fetched.url != clean_url and not await robots.allowed(client, fetched.url):
            return result("blocked", error="redirect target disallowed by robots.txt")

        if fetched.content_type in TEXT_TYPES:
            title, text = None, fetched.text().strip()
        elif fetched.content_type in HTML_TYPES or not fetched.content_type:
            title, text = extract_main_text(fetched.content, fetched.url)
        else:
            return result("unsupported_type", error=f"cannot read {fetched.content_type}")
        if not text:
            return result("empty", title=title, error="no main text found")
        return result(
            "ok", title=title, text=text[:max_chars], text_truncated=len(text) > max_chars
        )

    return mcp


def main() -> None:
    run_stdio(create_server())


if __name__ == "__main__":
    main()
