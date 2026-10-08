import httpx
import pytest

from context_window_mcp.http import USER_AGENT, FetchError, HttpConfig, PoliteClient


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler: httpx.MockTransport, time: FakeTime, **config: object) -> PoliteClient:
    return PoliteClient(
        HttpConfig(**config),  # pyright: ignore[reportArgumentType]
        transport=handler,
        sleep=time.sleep,
        clock=time.clock,
    )


async def test_sends_user_agent_and_extra_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    async with PoliteClient(transport=httpx.MockTransport(handler), headers={"X-A": "1"}) as client:
        await client.get("https://example.com/")
    assert seen[0].headers["user-agent"] == USER_AGENT
    assert seen[0].headers["x-a"] == "1"


async def test_waits_between_requests_to_the_same_host() -> None:
    time = FakeTime()
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    async with _client(transport, time, cache=False) as client:
        await client.get("https://export.arxiv.org/a")
        await client.get("https://export.arxiv.org/b")
        await client.get("https://other.example/c")  # another host: no wait
    assert time.sleeps == [pytest.approx(3.0)]


async def test_retries_5xx_then_succeeds() -> None:
    time = FakeTime()
    statuses = iter([503, 502, 200])
    transport = httpx.MockTransport(lambda r: httpx.Response(next(statuses), text="x"))
    async with _client(transport, time, default_interval_seconds=0) as client:
        fetched = await client.get("https://example.com/")
    assert fetched.status == 200
    assert time.sleeps == [1.0, 2.0]


async def test_honours_retry_after_but_caps_it() -> None:
    time = FakeTime()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "999"}),
            httpx.Response(200),
        ]
    )
    transport = httpx.MockTransport(lambda r: next(responses))
    async with _client(transport, time, default_interval_seconds=0) as client:
        assert (await client.get("https://example.com/")).status == 200
    assert time.sleeps == [7.0, 30.0]


async def test_gives_back_the_last_error_status_after_retries() -> None:
    time = FakeTime()
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    async with _client(transport, time, default_interval_seconds=0) as client:
        fetched = await client.get("https://example.com/")
    assert fetched.status == 503
    assert not fetched.ok


async def test_network_failure_raises_after_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("refused", request=request)

    async with _client(
        httpx.MockTransport(handler), FakeTime(), default_interval_seconds=0
    ) as client:
        with pytest.raises(FetchError, match="ConnectError"):
            await client.get("https://example.com/")
    assert attempts == 3


async def test_does_not_retry_404() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404)

    async with _client(httpx.MockTransport(handler), FakeTime()) as client:
        assert (await client.get("https://example.com/")).status == 404
    assert attempts == 1


async def test_stops_reading_at_the_size_limit() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    async with _client(transport, FakeTime(), max_bytes=1000) as client:
        fetched = await client.get("https://example.com/")
    assert len(fetched.content) == 1000
    assert fetched.truncated


async def test_caches_successful_gets_for_the_process() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="cached")

    async with _client(httpx.MockTransport(handler), FakeTime()) as client:
        await client.get("https://example.com/", params={"b": 2, "a": 1})
        await client.get("https://example.com/", params={"a": 1, "b": 2})
    assert calls == 1


async def test_does_not_cache_errors() -> None:
    statuses = iter([404, 200])
    transport = httpx.MockTransport(lambda r: httpx.Response(next(statuses)))
    async with _client(transport, FakeTime()) as client:
        assert (await client.get("https://example.com/")).status == 404
        assert (await client.get("https://example.com/")).status == 200


async def test_reports_final_url_after_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text="here")

    async with _client(
        httpx.MockTransport(handler), FakeTime(), default_interval_seconds=0
    ) as client:
        fetched = await client.get("https://example.com/old")
    assert fetched.url == "https://example.com/new"
