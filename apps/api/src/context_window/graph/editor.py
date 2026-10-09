"""The editor-in-chief: `plan_coverage` decides what each reporter looks for today.

One assignment goes to every enabled beat (a beat is enabled when it has at least one
enabled source; PLAN.md 6.3). The strong model writes each beat's focus from the
watchlist, the reader's interests and yesterday's headlines. If that call fails, the
run goes on with default focuses: an editor's bad day must not stop the paper.
"""

import logging
import time
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from sqlalchemy import select

from context_window.budget import BudgetExceeded
from context_window.db.models import Briefing as BriefingRow
from context_window.db.models import Interest, Source, Story, WatchlistRepo
from context_window.graph.context import CoveragePlanner, NewsroomContext
from context_window.graph.reporter.community import DEFAULT_KEYWORDS
from context_window.graph.scoring import interest_vectors
from context_window.graph.state import NewsroomState, NewsroomUpdate
from context_window.models import with_network_retry
from context_window.schemas.agents import CoverageBrief, CoveragePlan
from context_window.schemas.enums import Beat, BriefingStatus, StoryStatus
from context_window.schemas.newsroom import BeatAssignment, RunError, RunStats

log = logging.getLogger(__name__)

DEFAULT_FOCUS: dict[Beat, str] = {
    Beat.RELEASES: (
        "Changes in the watchlist tools that affect engineers who use them: new features, "
        "breaking changes, deprecations and security fixes."
    ),
    Beat.RESEARCH: "Papers with concrete results that engineers building with AI can use.",
    Beat.INDUSTRY: "Announcements of models, APIs, products and techniques engineers can use.",
    Beat.COMMUNITY: "Articles that tell engineers something concrete about building with AI.",
}
MAX_KEYWORDS = 40

SYSTEM = """\
You are the editor-in-chief of Context Window, a daily briefing for AI engineers. Each \
morning you tell your reporters what to look for. You will get the beats to cover, the \
tools on the reader's watchlist, the reader's interests and yesterday's headlines.

The reader is an engineer: they want technical substance (models, APIs, tools, \
techniques, results) over business news such as funding, partnerships or hiring.

Write one assignment per beat listed:
- focus: one or two sentences on what that reporter should prioritise today, grounded in \
the reader's interests and watchlist. Ask for new developments, not follow-ups to \
yesterday's headlines unless something material changed.
- keywords: for the community beat only, 15 to 40 words or short phrases that a \
relevant Hacker News title would contain (names of models, labs, tools and techniques). \
For every other beat, an empty list.
"""


def render_brief(brief: CoverageBrief) -> str:
    lines = [f"Date: {brief.run_date.isoformat()}", "Beats: " + ", ".join(brief.beats), ""]
    lines.append("Watchlist: " + (", ".join(brief.watchlist) or "(none)"))
    lines.append("Interests:")
    lines.extend(f"- {interest}" for interest in brief.interests or ["(none)"])
    lines.append("Yesterday's headlines:")
    lines.extend(f"- {headline}" for headline in brief.yesterday or ["(no briefing yet)"])
    return "\n".join(lines)


def make_coverage_planner(model: BaseChatModel) -> CoveragePlanner:
    structured = with_network_retry(
        model.with_structured_output(
            CoveragePlan, method="json_schema", strict=True, include_raw=True
        )
    )

    async def plan(brief: CoverageBrief) -> CoveragePlan:
        result = cast(
            dict[str, Any],
            await structured.ainvoke(
                [SystemMessage(SYSTEM), HumanMessage(render_brief(brief))],
                config={"run_name": "plan_coverage_model"},
            ),
        )
        parsed = result.get("parsed")
        if not isinstance(parsed, CoveragePlan):
            raise RuntimeError(
                f"the editor's answer did not fit the schema: {result.get('parsing_error')}"
            )
        return parsed

    return plan


def build_assignments(
    beats: Sequence[Beat], plan: CoveragePlan | None, max_items: int
) -> list[BeatAssignment]:
    """One assignment per beat, in the given order. The plan's text is used where it has
    some; community keywords are added to the defaults, never replace them."""
    planned = {a.beat: a for a in plan.assignments} if plan else {}
    assignments: list[BeatAssignment] = []
    for beat in beats:
        entry = planned.get(beat)
        focus = " ".join(entry.focus.split()) if entry and entry.focus.strip() else ""
        keywords: list[str] = []
        if beat is Beat.COMMUNITY:
            extra = [k.strip() for k in (entry.keywords if entry else []) if k.strip()]
            keywords = list(dict.fromkeys([*DEFAULT_KEYWORDS, *extra]))[
                : len(DEFAULT_KEYWORDS) + MAX_KEYWORDS
            ]
        assignments.append(
            BeatAssignment(
                beat=beat,
                focus=focus or DEFAULT_FOCUS[beat],
                max_items=max_items,
                keywords=keywords,
            )
        )
    return assignments


async def plan_coverage(state: NewsroomState, runtime: Runtime[NewsroomContext]) -> NewsroomUpdate:
    started = time.perf_counter()
    ctx = runtime.context
    requested = state.get("beats") or list(Beat)
    async with ctx.db() as session:
        enabled = set(
            (await session.scalars(select(Source.beat).where(Source.enabled.is_(True)))).all()
        )
        # Embed any new or edited interests now, before the research desk filters by them.
        await interest_vectors(session, ctx.embed, ctx.settings.require_embedding_model())
        await session.commit()
        repos = (
            await session.execute(
                select(WatchlistRepo.owner, WatchlistRepo.repo).where(
                    WatchlistRepo.enabled.is_(True)
                )
            )
        ).all()
        interests = (await session.scalars(select(Interest).order_by(Interest.weight.desc()))).all()
        last = await session.scalar(
            select(BriefingRow.run_id)
            .where(BriefingRow.status == BriefingStatus.PUBLISHED)
            .order_by(BriefingRow.date.desc())
            .limit(1)
        )
        yesterday = (
            list(
                (
                    await session.scalars(
                        select(Story.headline).where(
                            Story.run_id == last, Story.status == StoryStatus.PUBLISHED
                        )
                    )
                ).all()
            )
            if last is not None
            else []
        )

    beats = [beat for beat in Beat if beat in requested and beat in enabled]
    brief = CoverageBrief(
        run_date=state["run_date"],
        beats=beats,
        watchlist=[f"{owner}/{repo}" for owner, repo in repos],
        interests=[f"{i.topic}: {i.description or i.topic}" for i in interests],
        yesterday=yesterday,
    )
    errors: list[RunError] = []
    plan: CoveragePlan | None = None
    if beats:
        try:
            plan = await ctx.agents.plan_coverage(brief)
        except BudgetExceeded as exc:
            errors.append(RunError(node="plan_coverage", source="budget", message=str(exc)))
        except Exception as exc:  # fall back to the default focuses
            log.exception("plan_coverage failed")
            errors.append(RunError(node="plan_coverage", message=f"{type(exc).__name__}: {exc}"))

    assignments = build_assignments(beats, plan, ctx.settings.max_items_per_beat)
    for assignment in assignments:
        log.info("assignment %s: %s", assignment.beat, assignment.focus)
    elapsed = round((time.perf_counter() - started) * 1000)
    return {
        "assignments": assignments,
        "stats": RunStats(errors=errors, timings_ms={"plan_coverage": elapsed}),
    }
