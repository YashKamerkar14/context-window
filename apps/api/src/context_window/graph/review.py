"""`human_review`: the draft waits for an editor's decision (PLAN.md 2 and 6.3).

In manual mode the node calls `interrupt()`: the run's state is already in the Postgres
checkpoint, the job marks the run `awaiting_review` and exits. `cw review` (or, later,
the API) saves a decision and resumes the run with it; LangGraph then re-runs this node
from the top, so nothing before `interrupt()` may write anything. A run left waiting is
approved by `cw review-timeout` after REVIEW_TIMEOUT_HOURS.

In auto mode (production) the draft is approved at once.

- approve: publish as is.
- edit: remove stories and replace edited ones; edited stories are marked "edited by the
  editor", since their new wording was not fact-checked. The intro goes back to the plain
  one, because it may mention a removed story.
- reject: the run ends and nothing is published.
"""

# LangGraph spans several distributions in one namespace, so pyright misses its py.typed
# marker, and its annotations contain unknown type arguments (e.g. CachePolicy[Unknown]).
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false

import uuid
from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from sqlalchemy import update

from context_window.db import models as db
from context_window.graph.briefing import LIGHT_DAY, default_intro, render_markdown, save_draft
from context_window.graph.context import NewsroomContext
from context_window.graph.state import NewsroomState, NewsroomUpdate
from context_window.schemas.enums import DropReason, StoryStatus
from context_window.schemas.newsroom import Briefing, ReviewDecision


def review_request(state: NewsroomState) -> dict[str, Any]:
    """What the reviewer is shown: enough to decide, with ids to remove or edit by."""
    draft = state.get("draft")
    stories = draft.stories if draft else []
    return {
        "run_id": state["run_id"],
        "date": state["run_date"].isoformat(),
        "intro": draft.intro if draft else "",
        "stories": [{"id": s.id, "beat": s.beat.value, "headline": s.headline} for s in stories],
    }


def apply_decision(draft: Briefing, decision: ReviewDecision) -> Briefing:
    if decision.action != "edit":
        return draft
    removed = set(decision.removed_story_ids)
    edited = {
        s.id: s.model_copy(update={"edited_by_editor": True}) for s in decision.edited_stories
    }
    stories = [edited.get(s.id, s) for s in draft.stories if s.id not in removed]
    light_day = len(stories) < LIGHT_DAY
    return draft.model_copy(
        update={
            "stories": stories,
            "light_day": light_day,
            "intro": default_intro(stories, light_day),
        }
    )


async def human_review(state: NewsroomState, runtime: Runtime[NewsroomContext]) -> NewsroomUpdate:
    ctx = runtime.context
    draft = state.get("draft")
    if ctx.settings.review_mode == "auto":
        return {"review": ReviewDecision(action="approve", decided_by="auto")}

    answer = interrupt(review_request(state))  # the run pauses here until resumed
    decision = ReviewDecision.model_validate(answer)
    if draft is None or decision.action != "edit":
        return {"review": decision}

    revised = apply_decision(draft, decision)
    run_id = uuid.UUID(state["run_id"])
    await save_draft(ctx, run_id, revised, render_markdown(revised))
    removed = [uuid.UUID(i) for i in decision.removed_story_ids]
    if removed:
        async with ctx.db() as session:
            await session.execute(
                update(db.Story)
                .where(db.Story.id.in_(removed))
                .values(status=StoryStatus.DROPPED, drop_reason=DropReason.REMOVED_BY_EDITOR)
            )
            await session.commit()
    return {"review": decision, "draft": revised}


def after_review(state: NewsroomState) -> Literal["publish", "__end__"]:
    review = state.get("review")
    return "__end__" if review is not None and review.action == "reject" else "publish"
