"""Inputs and structured outputs of the model-backed agents other than the reporters.

Model output schemas have no defaults or length limits, so every provider can enforce
them as strict JSON schemas; code checks the limits afterwards.
"""

import datetime as dt
from dataclasses import dataclass

from pydantic import BaseModel, Field

from context_window.schemas.enums import Beat

# --- Editor-in-chief (plan_coverage) ---------------------------------------------------


@dataclass(frozen=True)
class CoverageBrief:
    run_date: dt.date
    beats: list[Beat]
    watchlist: list[str]
    interests: list[str]
    yesterday: list[str]  # headlines of the last published briefing


class BeatFocus(BaseModel):
    beat: Beat
    focus: str = Field(description="One or two sentences: what this reporter should look for")
    keywords: list[str] = Field(
        description="Community beat only: words a relevant Hacker News title contains"
    )


class CoveragePlan(BaseModel):
    assignments: list[BeatFocus]


# --- Merge and rank --------------------------------------------------------------------


@dataclass(frozen=True)
class StoryPair:
    id: int
    first: str  # headline and summary
    second: str


class PairVerdict(BaseModel):
    pair_id: int
    same_event: bool


class PairVerdicts(BaseModel):
    verdicts: list[PairVerdict]


# --- Writer (write_briefing) -----------------------------------------------------------


class BriefingIntro(BaseModel):
    intro: str = Field(description="One sentence introducing today's briefing")
