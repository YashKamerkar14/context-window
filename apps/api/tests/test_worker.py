from context_window.worker import run_worker


async def test_worker_stops_after_max_beats() -> None:
    assert await run_worker(heartbeat_seconds=0, max_beats=3) == 3
