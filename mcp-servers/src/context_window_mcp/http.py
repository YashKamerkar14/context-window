"""A polite HTTP client shared by every server.

- Identifies itself with a descriptive User-Agent.
- Waits a minimum interval between requests to the same host (arXiv asks for 3 seconds).
- Retries timeouts, 429 and 5xx with backoff, honouring Retry-After.
- Stops reading a body at a size limit.
- Caches successful GETs for the life of the process. Each server process lives for one
  newsroom run, so this is the per-run cache the plan asks for.
"""

import asyncio
import email.utils
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

USER_AGENT = "ContextWindow/0.1 (+https://github.com/YashKamerkar14/context-window)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0


class FetchError(Exception):
    """The request could not be completed (network failure after retries)."""


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str = USER_AGENT
    timeout_seconds: float = 20.0
    max_bytes: int = 2_000_000
    retries: int = 2
    backoff_seconds: float = 1.0
    default_interval_seconds: float = 0.5
    host_intervals: Mapping[str, float] = field(
        default_factory=lambda: {"export.arxiv.org": 3.0, "arxiv.org": 3.0}
    )
    cache: bool = True

    @classmethod
    def from_env(cls) -> "HttpConfig":
        max_bytes = int(os.environ.get("MAX_FETCH_BYTES") or cls.max_bytes)
        return cls(max_bytes=max_bytes)


@dataclass(frozen=True)
class Fetched:
    url: str  # final URL after redirects
    status: int
    headers: httpx.Headers
    content: bytes
    truncated: bool

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        encoding = httpx.Response(self.status, headers=self.headers).encoding or "utf-8"
        return self.content.decode(encoding, errors="replace")


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    parsed = email.utils.parsedate_to_datetime(value)
    return max(0.0, parsed.timestamp() - time.time())


class PoliteClient:
    def __init__(
        self,
        config: HttpConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.config = config or HttpConfig()
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": self.config.user_agent, **(headers or {})},
            timeout=self.config.timeout_seconds,
            follow_redirects=True,
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._cache: dict[str, Fetched] = {}

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _interval(self, host: str) -> float:
        return self.config.host_intervals.get(host, self.config.default_interval_seconds)

    async def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self._interval(host) - (self._clock() - last)
            if wait > 0:
                await self._sleep(wait)
        self._last_request[host] = self._clock()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> Fetched:
        full_url = f"{url}?{urlencode(sorted(params.items()))}" if params else url
        if self.config.cache and full_url in self._cache:
            return self._cache[full_url]

        host = urlsplit(full_url).hostname or ""
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        limit = max_bytes or self.config.max_bytes
        attempt = 0
        while True:
            async with lock:
                await self._wait_turn(host)
                try:
                    fetched = await self._get_once(full_url, headers, limit)
                    error: Exception | None = None
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ) as exc:
                    fetched, error = None, exc

            retryable = error is not None or (
                fetched is not None and fetched.status in RETRY_STATUSES
            )
            if not retryable or attempt >= self.config.retries:
                break
            attempt += 1
            delay = self.config.backoff_seconds * 2 ** (attempt - 1)
            if fetched is not None:
                after = _retry_after(fetched.headers.get("retry-after"))
                if after is not None:
                    delay = min(after, MAX_RETRY_AFTER_SECONDS)
            await self._sleep(delay)

        if fetched is None:
            raise FetchError(f"{type(error).__name__} fetching {full_url}: {error}")
        if fetched.ok and self.config.cache:
            self._cache[full_url] = fetched
        return fetched

    async def _get_once(self, url: str, headers: Mapping[str, str] | None, limit: int) -> Fetched:
        async with self._client.stream("GET", url, headers=headers) as response:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    chunks.append(chunk[: len(chunk) - (size - limit)])
                    truncated = True
                    break
                chunks.append(chunk)
            return Fetched(
                url=str(response.url),
                status=response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                truncated=truncated,
            )
