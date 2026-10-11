"""Measure the published newsroom against the targets in PLAN.md 10.

    uv run --project apps/api python evals/run_evals.py            # report
    uv run --project apps/api python evals/run_evals.py --sample   # (re)draw the claim sample

Reads the published briefings from the API (the production site by default), so it
needs no database access. Embeddings use the model settings in `.env`; cost and run time
need ADMIN_TOKEN, since the API shows them only to the admin. Writes evals/results.md.

Two metrics need people: claim accuracy reads the `label` column of
evals/datasets/claim_accuracy.jsonl, and coverage reads the hand-made list of the week's
major AI stories in evals/datasets/coverage.jsonl.
"""

import argparse
import asyncio
import datetime as dt
import json
import re
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from context_window.config import Settings, get_settings
from context_window.embeddings import make_embedder
from context_window.evaluation import (
    Published,
    Rate,
    covered,
    duplicates,
    published_stories,
    quote_on_page,
    sample_claims,
    unverified_sentences,
)

EVALS = Path(__file__).resolve().parent
CLAIMS = EVALS / "datasets" / "claim_accuracy.jsonl"
COVERAGE = EVALS / "datasets" / "coverage.jsonl"
AGREEMENT = EVALS / "factcheck" / "agreement.md"
RESULTS = EVALS / "results.md"
DEFAULT_API = "https://context-window-snrd.vercel.app"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def pct(rate: Rate) -> str:
    return "n/a" if rate.share is None else f"{rate.share:.1%} ({rate.hits}/{rate.total})"


async def fetch_briefings(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    archive = (await client.get("/briefings", params={"limit": 90})).raise_for_status().json()
    briefings: list[dict[str, Any]] = []
    for entry in archive:
        response = await client.get(f"/briefings/{entry['date']}")
        briefings.append(response.raise_for_status().json())
    return briefings


async def fetch_runs(client: httpx.AsyncClient, settings: Settings) -> list[dict[str, Any]]:
    if settings.admin_token is None:
        return []
    headers = {"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"}
    response = await client.get("/runs", params={"limit": 50}, headers=headers)
    return response.raise_for_status().json()


async def draw_claim_sample(stories: Sequence[Published], size: int) -> None:
    """Pick the claims to hand-check, keep any labels already given, and check each
    evidence quote against its page."""
    old = {row["key"]: row for row in read_jsonl(CLAIMS)}
    rows = sample_claims(stories, size)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        for row in rows:
            for field in ("label", "note", "reviewer"):
                row[field] = old.get(row["key"], {}).get(field)
            # A quote taken from a feed (arXiv's RSS abstract, say) is formatted differently
            # from the web page, so only quotes taken from the page are checked against it.
            row["quote_on_page"] = None
            if row["evidence_kind"] == "page" and row["evidence_quote"]:
                try:
                    page = (await client.get(row["evidence_url"])).text
                    row["quote_on_page"] = quote_on_page(row["evidence_quote"], page)
                except httpx.HTTPError:
                    pass
    write_jsonl(CLAIMS, rows)
    print(f"Wrote {len(rows)} claims to {CLAIMS.relative_to(EVALS.parent)}; label each one.")


def agreement() -> str:
    match = re.search(r"Agreement: (\d+) of (\d+)", AGREEMENT.read_text(encoding="utf-8"))
    return pct(Rate(int(match[1]), int(match[2]))) if match else "n/a"


def claim_accuracy() -> tuple[str, str]:
    rows = read_jsonl(CLAIMS)
    labelled = [r for r in rows if r.get("label") in ("supported", "unsupported")]
    accuracy = Rate(sum(r["label"] == "supported" for r in labelled), len(labelled))
    checked = [r for r in rows if r.get("quote_on_page") is not None]
    grounded = Rate(sum(bool(r["quote_on_page"]) for r in checked), len(checked))
    return pct(accuracy), pct(grounded)


async def coverage(
    stories: Sequence[Published], vectors: dict[str, list[float]], settings: Settings
) -> tuple[Rate, list[str]]:
    listed = read_jsonl(COVERAGE)
    if not listed or not stories:
        return Rate(0, 0), []
    first, last = stories[0].date, stories[-1].date
    listed = [r for r in listed if first <= dt.date.fromisoformat(r["date"]) <= last]
    embed = make_embedder(settings)
    listed_vectors = await embed([r["title"] for r in listed]) if listed else []
    missed: list[str] = []
    for row, vector in zip(listed, listed_vectors, strict=True):
        hit = covered(
            vector,
            stories,
            vectors,
            threshold=settings.merge_ask_threshold,
            keywords=row.get("keywords") or [],
        )
        if hit is None:
            missed.append(row["title"])
    return Rate(len(listed) - len(missed), len(listed)), missed


def run_numbers(runs: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """Cost of published runs, and wall-clock time of those that did not pause for
    review (a pause can last hours and is not the newsroom's own time)."""
    published = [r for r in runs if r["status"] == "published" and r["finished_at"]]
    costs = [float(r["stats"]["cost_usd"]) for r in published if r["stats"].get("cost_usd")]
    done = [r for r in published if not r["review_requested_at"]]
    minutes = [
        (
            dt.datetime.fromisoformat(r["finished_at"]) - dt.datetime.fromisoformat(r["started_at"])
        ).total_seconds()
        / 60
        for r in done
    ]
    cost = f"${statistics.mean(costs):.3f} mean (n={len(costs)})" if costs else "n/a"
    time = f"{statistics.mean(minutes):.1f} min mean (n={len(minutes)})" if minutes else "n/a"
    return cost, time


async def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the newsroom against PLAN.md 10.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--sample", action="store_true", help="draw the claim sample to label")
    parser.add_argument("--sample-size", type=int, default=50)
    args = parser.parse_args()
    settings = get_settings()

    async with httpx.AsyncClient(base_url=args.api.rstrip("/"), timeout=30) as client:
        briefings = await fetch_briefings(client)
        runs = await fetch_runs(client, settings)
    stories = published_stories(briefings)
    if args.sample:
        await draw_claim_sample(stories, args.sample_size)

    embed = make_embedder(settings)
    vectors = dict(
        zip([s.id for s in stories], await embed([s.text() for s in stories]), strict=True)
    )
    unverified, unverified_examples = unverified_sentences(stories)
    repeats = duplicates(stories, vectors, threshold=settings.merge_threshold)
    duplicate_rate = Rate(len({d.later.id for d in repeats}), len(stories))
    accuracy, grounded = claim_accuracy()
    coverage_rate, missed = await coverage(stories, vectors, settings)
    cost, time = run_numbers(runs)

    period = f"{stories[0].date} to {stories[-1].date}" if stories else "no editions"
    table = [
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Fact-checker agreement (60 labelled claims) | {agreement()} | 90% or higher |",
        f"| Claim accuracy (hand-checked sample) | {accuracy} | 95% or higher |",
        f"| Evidence quote found on its page (automatic) | {grounded} | (supporting check) |",
        f"| Unverified sentence rate | {pct(unverified)} | under 2% |",
        f"| Duplicate rate (14-day window) | {pct(duplicate_rate)} | under 3% |",
        f"| Coverage of the week's major AI stories | {pct(coverage_rate)} | 70% or higher |",
        f"| Cost per run | {cost} | about $0.80 |",
        f"| Run time | {time} | report |",
    ]
    today = dt.datetime.now(dt.UTC).date()
    details = [
        (
            f"# Evaluation results\n\nMeasured {today} over {len(briefings)} editions"
            f" ({period}), {len(stories)} published stories. Produced by `evals/run_evals.py`.\n"
        ),
        *table,
        "\n## Unverified sentences\n",
        *([f"- {e}" for e in unverified_examples] or ["None."]),
        "\n## Repeated stories\n",
        *(
            [
                f'- {d.later.date} "{d.later.headline}" repeats {d.earlier.date}'
                f' "{d.earlier.headline}" ({d.reason})'
                for d in repeats
            ]
            or ["None."]
        ),
        "\n## Major stories not covered\n",
        *([f"- {m}" for m in missed] or ["None." if coverage_rate.total else "No list yet."]),
    ]
    RESULTS.write_text("\n".join(details) + "\n", encoding="utf-8")
    print("\n".join(table))
    print(f"\nDetails: {RESULTS.relative_to(EVALS.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
