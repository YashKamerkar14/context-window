"""The `cw` command."""

import argparse
import asyncio
import logging

from context_window.config import get_settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cw", description="Context Window newsroom")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="serve the HTTP API")
    api.add_argument("--reload", action="store_true", help="reload on code changes")

    worker = sub.add_parser("worker", help="run the background worker")
    worker.add_argument("--once", action="store_true", help="heartbeat once and exit")
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "api":
        import uvicorn

        uvicorn.run(
            "context_window.api.app:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=args.reload,
        )
    elif args.command == "worker":
        max_beats = 1 if args.once else None
        asyncio.run(run_worker_entry(settings.worker_heartbeat_seconds, max_beats))


async def run_worker_entry(heartbeat_seconds: float, max_beats: int | None) -> None:
    from context_window.worker import run_worker

    await run_worker(heartbeat_seconds, max_beats)
