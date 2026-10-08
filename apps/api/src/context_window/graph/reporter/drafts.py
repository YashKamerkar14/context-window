"""What a writer sees and what it returns.

`StoryDrafts` is the model's structured output. It is kept simple (no defaults, no
length limits) so every provider can enforce it as a strict JSON schema; the limits are
checked afterwards, when a draft becomes a `StoryCandidate`. The model never supplies a
URL or a key: those come from the lead it cites by id.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class LeadText:
    """One lead as the writer sees it: an id local to the batch, and the text."""

    id: int
    title: str
    published: dt.date | None
    text: str
    labels: Mapping[str, str] = field(default_factory=dict[str, str])


class DraftSentence(BaseModel):
    text: str = Field(description="One sentence of the summary, in your own words")
    claim_ids: list[str] = Field(description="Ids of the claims this sentence states")


class DraftClaim(BaseModel):
    id: str = Field(description='"c1", "c2", ... unique within the story')
    text: str = Field(description="One short fact, checkable against the item's text alone")


class DraftStory(BaseModel):
    item_id: int = Field(description="The id of the item this story is about")
    headline: str
    summary: list[DraftSentence] = Field(description="One or two sentences")
    why_it_matters: str
    claims: list[DraftClaim] = Field(description="One to four claims")
    is_breaking_change: bool


class SkippedItem(BaseModel):
    item_id: int
    reason: str


class StoryDrafts(BaseModel):
    stories: list[DraftStory]
    skipped: list[SkippedItem]
