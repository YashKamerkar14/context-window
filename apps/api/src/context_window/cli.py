"""The `cw` command."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from context_window import aio
from context_window.config import get_settings

if TYPE_CHECKING:
    from context_window.runner import RunResult
    from context_window.settings_file import SettingsFile, SyncReport

log = logging.getLogger("cw")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cw", description="Context Window newsroom")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="serve the HTTP API")
    api.add_argument("--reload", action="store_true", help="reload on code changes")

    run = sub.add_parser("run", help="run the newsroom once")
    run.add_argument(
        "--beat",
        action="append",
        choices=["releases", "research", "industry", "community"],
        help="beat to report (repeatable; default: every enabled beat)",
    )
    run.add_argument(
        "--consume",
        action="store_true",
        help=(
            "dev only: mark the draft briefing's items as consumed, as publishing will, so "
            "the next run skips them"
        ),
    )
    run.add_argument(
        "--claims", action="store_true", help="also print each story's claims and claim map"
    )

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

    factcheck = sub.add_parser("factcheck", help="the fact-checker's labelled claims")
    factcheck_sub = factcheck.add_subparsers(dest="factcheck_command", required=True)
    dataset = factcheck_sub.add_parser(
        "dataset", help="build the labelled claims from saved stories"
    )
    dataset.add_argument("--force", action="store_true", help="replace the existing dataset")
    factcheck_sub.add_parser("eval", help="measure agreement on the labelled claims")

    calibrate = sub.add_parser("calibrate", help="calibrate the merge thresholds")
    calibrate_sub = calibrate.add_subparsers(dest="calibrate_command", required=True)
    pairs = calibrate_sub.add_parser("pairs", help="build labelled story pairs from saved stories")
    pairs.add_argument("--force", action="store_true", help="replace an existing pairs file")
    calibrate_sub.add_parser("thresholds", help="sweep thresholds and write the report")

    mcp = sub.add_parser("mcp", help="call the MCP source servers directly")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_sub.add_parser("list", help="list every server and its tools")
    call = mcp_sub.add_parser("call", help="call one tool and print its JSON result")
    call.add_argument(
        "server", help="rss, github_releases, arxiv, hf_papers, hackernews, web_fetch"
    )
    call.add_argument("tool", help="tool name, e.g. fetch_feed")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object of arguments")
    return parser


def main(argv: list[str] | None = None) -> None:
    # Windows consoles default to a legacy code page that cannot print many headlines.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    args = _build_parser().parse_args(argv)

    if args.command == "api":
        _serve_api(reload=args.reload)
    elif args.command == "run":
        _run(args.beat or [], consume=args.consume, claims=args.claims)
    elif args.command == "worker":
        from context_window.worker import run_worker

        aio.run(run_worker(get_settings().worker_heartbeat_seconds, 1 if args.once else None))
    elif args.command == "db":
        _db(args.db_command)
    elif args.command == "settings":
        _load_settings(args.file, reset_weights=args.reset_weights, dry_run=args.dry_run)
    elif args.command == "factcheck":
        aio.run(_factcheck(args.factcheck_command, force=getattr(args, "force", False)))
    elif args.command == "calibrate":
        aio.run(_calibrate(args.calibrate_command, force=getattr(args, "force", False)))
    elif args.command == "mcp":
        aio.run(
            _mcp(
                args.mcp_command,
                getattr(args, "server", ""),
                getattr(args, "tool", ""),
                getattr(args, "arguments", "{}"),
            )
        )


async def _mcp(command: str, server: str, tool: str, arguments: str) -> None:
    from context_window.mcp_client import NewsroomTools, known_servers

    async with NewsroomTools() as tools:
        if command == "list":
            for name in known_servers():
                print(f"{name}: {', '.join(await tools.list_tools(name))}")
            return
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"arguments must be a JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("arguments must be a JSON object")
        result = await tools.call(server, tool, parsed)  # pyright: ignore[reportUnknownArgumentType]
        print(json.dumps(result, indent=2, ensure_ascii=False))


def _run(beats: list[str], *, consume: bool, claims: bool) -> None:
    from context_window.db.queries import RunAlreadyActiveError
    from context_window.runner import run_newsroom
    from context_window.schemas.enums import Beat

    settings = get_settings()
    selected = [Beat(b) for b in dict.fromkeys(beats)]
    try:
        result = aio.run(run_newsroom(settings, selected, consume=consume))
    except RunAlreadyActiveError as exc:
        raise SystemExit(f"{exc}. Wait for it to finish, or mark it failed.") from exc
    _print_run(result, claims=claims)


def _print_run(result: "RunResult", *, claims: bool = False) -> None:
    print()
    print(f"Run {result.run_id} ({result.run_date})")
    print(
        f"{len(result.candidates)} candidates -> {len(result.stories)} stories in the "
        f"fact-check pool -> {len(result.draft.stories) if result.draft else 0} in the draft"
    )
    print()
    draft_ids = {s.id for s in result.draft.stories} if result.draft else set[str]()
    for number, story in enumerate(result.stories, start=1):
        breaking = "  [BREAKING]" if story.is_breaking_change else ""
        merged = f", merged from {story.merged_from}" if story.merged_from > 1 else ""
        in_draft = "*" if story.id in draft_ids else " "
        print(f"{in_draft}{number:>2}. [{story.beat}] {story.headline}{breaking}")
        print(f"     score {story.score:.3f}{merged}")
        print(f"     {story.summary}")
        print(f"     Why it matters: {story.why_it_matters}")
        if claims:
            for entry in story.sentence_claim_map:
                print(f'     - "{entry.sentence}" <- {", ".join(entry.claim_ids)}')
            for claim in story.claims:
                print(f"       {claim.id}: {claim.text}")
        print(f"     Sources: {', '.join(story.source_urls)}")
        print()
    if result.draft is not None:
        light = " (light day)" if result.draft.light_day else ""
        print(f"* in the draft briefing{light}. Intro: {result.draft.intro}")
        print()

    stats, budget = result.stats, result.budget
    fetched = ", ".join(f"{beat} {n}" for beat, n in stats.items_fetched_by_beat.items())
    print(f"Items fetched: {fetched or 'none'}")
    print(
        f"Model calls: {stats.model_calls}, tokens in {stats.tokens_in:,}, out {stats.tokens_out:,}"
    )
    print(
        f"Cost: ${budget.cost_usd:.5f} by the price table, "
        f"${budget.reported_cost_usd:.5f} reported by the provider"
    )
    print("Timings: " + ", ".join(f"{node} {ms} ms" for node, ms in stats.timings_ms.items()))
    print(f"Trace: {result.trace_url or 'not available (is LANGSMITH_API_KEY set?)'}")
    if result.consumed_items:
        print(f"Marked {result.consumed_items} items consumed (--consume)")
    for error in stats.errors:
        source = f" [{error.source}]" if error.source else ""
        print(f"Error in {error.node}{source}: {error.message}")


async def _factcheck(command: str, *, force: bool) -> None:
    import datetime as dt

    from context_window.budget import Budget, use_budget
    from context_window.db.session import make_engine, make_sessionmaker
    from context_window.embeddings import make_embedder
    from context_window.factcheck import dataset
    from context_window.factcheck.judge import make_claim_judge
    from context_window.mcp_client import NewsroomTools
    from context_window.models import chat_model

    settings = get_settings()
    budget = Budget(prices=settings.model_prices, max_cost_usd=0.50, max_calls=300)
    with use_budget(budget):
        if command == "dataset":
            if dataset.CLAIMS_FILE.exists() and not force:
                raise SystemExit(f"{dataset.CLAIMS_FILE} exists; pass --force to replace it")
            engine = make_engine(settings.require_direct_url(), null_pool=True)
            try:
                async with make_sessionmaker(engine)() as session:
                    claims = await dataset.candidate_claims(session)
            finally:
                await engine.dispose()
            async with NewsroomTools(settings=settings) as tools:
                pages, labelled = await dataset.build(claims, tools, chat_model(settings, "strong"))
            dataset.write_jsonl(pages, dataset.PAGES_FILE)
            dataset.write_jsonl(labelled, dataset.CLAIMS_FILE)
            print(f"wrote {len(labelled)} claims about {len(pages)} pages to {dataset.CLAIMS_FILE}")
        else:
            fast = make_claim_judge(chat_model(settings, "fast"), run_name="judge_claims")
            strong = make_claim_judge(chat_model(settings, "strong"), run_name="judge_unclear")
            outcomes = await dataset.evaluate(
                dataset.read_pages(), dataset.read_claims(), make_embedder(settings), fast, strong
            )
            text = dataset.report(
                outcomes, model=settings.model_name("fast"), today=dt.date.today().isoformat()
            )
            dataset.REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            dataset.REPORT_FILE.write_text(text, encoding="utf-8")
            agree = sum(o.agrees for o in outcomes)
            print(f"agreement: {agree}/{len(outcomes)}; report: {dataset.REPORT_FILE}")
    print(f"cost: ${budget.cost_usd:.5f} ({budget.calls_started} calls)")


async def _calibrate(command: str, *, force: bool) -> None:
    import datetime as dt

    from context_window import calibration
    from context_window.budget import Budget, use_budget
    from context_window.db.session import make_engine, make_sessionmaker
    from context_window.embeddings import make_embedder
    from context_window.graph.rank import make_same_event_judge
    from context_window.models import chat_model

    settings = get_settings()
    embed = make_embedder(settings)
    budget = Budget(prices=settings.model_prices, max_cost_usd=0.50, max_calls=200)
    with use_budget(budget):
        if command == "pairs":
            if calibration.PAIRS_FILE.exists() and not force:
                raise SystemExit(f"{calibration.PAIRS_FILE} exists; pass --force to replace it")
            engine = make_engine(settings.require_direct_url(), null_pool=True)
            try:
                async with make_sessionmaker(engine)() as session:
                    stories = await calibration.load_stories(session)
            finally:
                await engine.dispose()
            if len(stories) < 10:
                raise SystemExit(f"only {len(stories)} saved stories; run the newsroom first")
            built = await calibration.build_pairs(stories, chat_model(settings, "fast"), embed)
            calibration.write_pairs(built)
            same = sum(p.same_event for p in built)
            print(f"wrote {len(built)} pairs ({same} same event) to {calibration.PAIRS_FILE}")
        else:
            pairs = calibration.read_pairs()
            scored = await calibration.score_pairs(pairs, embed)
            thresholds = calibration.choose_thresholds(scored)
            judge = make_same_event_judge(chat_model(settings, "fast"))
            judged = await calibration.judge_hardest(scored, judge)
            text = calibration.report(settings, scored, thresholds, judged, today=dt.date.today())
            calibration.REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            calibration.REPORT_FILE.write_text(text, encoding="utf-8", newline="\n")
            print(calibration.dumps(thresholds))
            print(f"report: {calibration.REPORT_FILE}")
    print(f"cost: ${budget.cost_usd:.5f} ({budget.calls_started} calls)")


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
