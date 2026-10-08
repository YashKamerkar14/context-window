"""Worker process. For now it only heartbeats; it will claim and run queued runs later."""

import asyncio
import logging
from datetime import UTC, datetime

log = logging.getLogger(__name__)


async def run_worker(heartbeat_seconds: float, max_beats: int | None = None) -> int:
    """Heartbeat until cancelled, or until max_beats is reached. Returns the beat count."""
    beats = 0
    while max_beats is None or beats < max_beats:
        beats += 1
        log.info("worker heartbeat %d at %s", beats, datetime.now(UTC).isoformat())
        if max_beats is not None and beats >= max_beats:
            break
        await asyncio.sleep(heartbeat_seconds)
    return beats
