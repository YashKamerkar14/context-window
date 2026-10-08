"""Graph state and reducers (PLAN.md section 6.1).

Fields that parallel branches write (`candidates`, `verified`, `dropped`, `stats`) have
reducers that combine every branch's update; the rest are replaced by the last writer.
Raw items and page text never enter this state: they stay in the subgraphs' private state
or in the database.
"""

import datetime as dt
import operator
from typing import Annotated, TypedDict

from context_window.schemas.newsroom import (
    BeatAssignment,
    Briefing,
    DroppedStory,
    ReviewDecision,
    RunStats,
    Story,
    StoryCandidate,
)


def merge_stats(left: RunStats | None, right: RunStats | None) -> RunStats:
    if left is None:
        return right or RunStats()
    if right is None:
        return left
    return left.merge(right)


class NewsroomState(TypedDict, total=False):
    run_id: str
    run_date: dt.date
    assignments: list[BeatAssignment]
    candidates: Annotated[list[StoryCandidate], operator.add]
    stories: list[Story]
    verified: Annotated[list[Story], operator.add]
    dropped: Annotated[list[DroppedStory], operator.add]
    draft: Briefing | None
    review: ReviewDecision | None
    stats: Annotated[RunStats, merge_stats]
