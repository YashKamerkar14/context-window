"""The `cw` command."""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from context_window import aio
from context_window.config import get_settings

if TYPE_CHECKING:
    from context_window.settings_file import SettingsFile, SyncReport

log = logging.getLogger("cw")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cw", description="Context Window newsroom")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="serve the HTTP API")
    api.add_argument("--reload", action="store_true", help="reload on code changes")

    worker = sub.add_parser("worker", help="run the background worker")
    worker.add_argument("--once", action="store_true", help="heartbeat once and exit")

    db = sub.add_parser("db", help="database setup")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="run migrations and set up the LangGraph checkpointer")
    db_sub.add_parser("status", help="show the database version, migration and pgvector")

    settings = sub.add_parser("settings", help="sources, watchlist and interests")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)
    load = settings_sub.add_parser("load", help="apply the settings file to the database")
    load.add_argument("--file", type=Path, help="settings file (default: config/settings.toml)")
    load.add_argument(
        "--reset-weights",
        action="store_true",
        help="reset interest weights to the file's values, discarding learned feedback",
    )
    load.add_argument("--dry-run", action="store_true", help="validate the file only")
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    args = _build_parser().parse_args(argv)

    if args.command == "api":
        _serve_api(reload=args.reload)
    elif args.command == "worker":
        from context_window.worker import run_worker

        aio.run(run_worker(get_settings().worker_heartbeat_seconds, 1 if args.once else None))
    elif args.command == "db":
        _db(args.db_command)
    elif args.command == "settings":
        _load_settings(args.file, reset_weights=args.reset_weights, dry_run=args.dry_run)


def _serve_api(*, reload: bool) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "context_window.api.app:app", host=settings.api_host, port=settings.port, reload=reload
    )


def _db(command: str) -> None:
    from context_window.db import migrate

    if command == "init":
        status = migrate.init_db()
        print("database ready")
    else:
        status = aio.run(migrate.inspect(get_settings().require_direct_url()))
        migrate.check_embedding_dim(status, get_settings().embedding_dim)
    print(f"  postgres:   {status.server_version}")
    print(f"  migration:  {status.revision or 'none (run `cw db init`)'}")
    print(f"  pgvector:   {status.pgvector_version or 'not installed'}")
    print(f"  embeddings: {status.embedding_column_type or '-'}")


def _load_settings(path: Path | None, *, reset_weights: bool, dry_run: bool) -> None:
    from context_window.settings_file import SettingsFile

    file = path or get_settings().settings_file
    parsed = SettingsFile.load(file)
    print(
        f"{file}: {len(parsed.sources)} sources, {len(parsed.watchlist)} repos, "
        f"{len(parsed.interests)} interests"
    )
    if dry_run:
        print("valid (dry run, nothing written)")
        return
    report = aio.run(_sync(parsed, reset_weights=reset_weights))
    print(
        f"loaded: {report.sources} sources ({report.sources_disabled} disabled), "
        f"{report.repos} repos ({report.repos_disabled} disabled), "
        f"{report.interests} interests ({report.interests_deleted} deleted)"
    )


async def _sync(parsed: "SettingsFile", *, reset_weights: bool) -> "SyncReport":
    from context_window.db.session import make_engine, make_sessionmaker
    from context_window.settings_file import sync_settings

    engine = make_engine(get_settings().require_direct_url(), null_pool=True)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            return await sync_settings(session, parsed, reset_weights=reset_weights)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
