"""The newsroom's core schemas (PLAN.md section 6.2).

Every agent returns one of these, never free text. Validation here is the contract between
the model's output and the rest of the graph: a candidate that does not validate is
dropped with an error rather than passed along.
"""

import datetime as dt
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from context_window.schemas.enums import Beat, DropReason, EvidenceKind, Verdict

MAX_SUMMARY_SENTENCES = 2


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeatAssignment(_Model):
    beat: Beat
    focus: str = Field(description="What the reporter should look for today")
    max_items: int = Field(ge=1, le=200)
    # Community beat: a Hacker News title must contain one of these (PLAN.md 2).
    keywords: list[str] = Field(default_factory=list[str])


class Claim(_Model):
    id: str = Field(min_length=1, max_length=20, description="Unique within its story")
    text: str = Field(min_length=1)
    source_url: str = Field(description="The primary source the claim is checked against")
    verdict: Verdict | None = None
    evidence_url: str | None = None
    evidence_quote: str | None = Field(default=None, max_length=300)
    evidence_kind: EvidenceKind | None = None
    reasoning: str | None = None
    attempts: int = Field(default=0, ge=0)


class SentenceClaims(_Model):
    """One factual sentence of the summary and the claims that back it."""

    sentence: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)


def fit_summary(sentences: list[SentenceClaims]) -> list[SentenceClaims]:
    """At most MAX_SUMMARY_SENTENCES entries: extra sentences join the last allowed one,
    keeping every claim they cite. Models sometimes write three sentences; dropping the
    story for it would waste a checked story, and dropping a sentence would hide a claim."""
    if len(sentences) <= MAX_SUMMARY_SENTENCES:
        return sentences
    keep, rest = sentences[: MAX_SUMMARY_SENTENCES - 1], sentences[MAX_SUMMARY_SENTENCES - 1 :]
    merged = SentenceClaims(
        sentence=" ".join(s.sentence for s in rest),
        claim_ids=list(dict.fromkeys(i for s in rest for i in s.claim_ids)),
    )
    return [*keep, merged]


class StoryCandidate(_Model):
    beat: Beat
    headline: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    claims: list[Claim] = Field(min_length=1, max_length=4)
    # Covers the summary exactly, sentence by sentence and in order.
    sentence_claim_map: list[SentenceClaims] = Field(min_length=1, max_length=MAX_SUMMARY_SENTENCES)
    source_urls: list[str] = Field(min_length=1)
    canonical_keys: list[str] = Field(min_length=1)
    published_at: dt.datetime | None = None
    is_breaking_change: bool = False

    @model_validator(mode="after")
    def _claims_are_mapped(self) -> Self:
        ids = [claim.id for claim in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("claim ids must be unique within a story")
        cited = {claim_id for entry in self.sentence_claim_map for claim_id in entry.claim_ids}
        if unknown := cited - set(ids):
            raise ValueError(f"sentences cite unknown claims: {sorted(unknown)}")
        if uncited := set(ids) - cited:
            raise ValueError(f"claims not cited by any sentence: {sorted(uncited)}")
        if " ".join(entry.sentence for entry in self.sentence_claim_map) != self.summary:
            raise ValueError("sentence_claim_map must cover the summary exactly")
        if self.is_breaking_change and self.beat is not Beat.RELEASES:
            raise ValueError("is_breaking_change applies to the releases beat only")
        return self


class Story(StoryCandidate):
    id: str
    score: float = Field(ge=0.0, le=1.0)
    merged_from: int = Field(default=1, ge=1)
    attempts: int = Field(default=0, ge=0)
    # Changed by the human reviewer after fact-checking; shown as such (PLAN.md 6.3).
    edited_by_editor: bool = False


class ClaimVerdict(_Model):
    claim_id: str
    verdict: Verdict
    evidence_url: str | None = None
    evidence_quote: str | None = Field(default=None, max_length=300)
    evidence_kind: EvidenceKind | None = None
    reasoning: str = Field(max_length=500)


class DroppedStory(_Model):
    story_id: str
    headline: str
    beat: Beat
    reason: DropReason
    claims: list[Claim] = Field(default_factory=list[Claim])


class RunError(_Model):
    node: str
    source: str | None = None
    message: str


class RunStats(_Model):
    """Counters for one run. Parallel branches each return their own and `merge` adds them."""

    items_fetched_by_beat: dict[Beat, int] = Field(default_factory=dict[Beat, int])
    candidates: int = 0
    merged: int = 0
    checked: int = 0
    verified: int = 0
    dropped: int = 0
    claims_supported: int = 0
    claims_unsupported: int = 0
    claims_unclear: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    errors: list[RunError] = Field(default_factory=list[RunError])
    timings_ms: dict[str, int] = Field(default_factory=dict[str, int])

    def merge(self, other: "RunStats") -> "RunStats":
        """Sum the numbers, concatenate the errors and add up timings per node."""
        merged: dict[str, object] = {}
        for name in type(self).model_fields:
            mine, theirs = getattr(self, name), getattr(other, name)
            if isinstance(mine, list):
                merged[name] = [*mine, *theirs]
            elif isinstance(mine, dict):
                merged[name] = _sum_dicts(mine, theirs)  # pyright: ignore[reportUnknownArgumentType]
            else:
                merged[name] = mine + theirs
        return type(self).model_validate(merged)


def _sum_dicts[K](a: dict[K, int], b: dict[K, int]) -> dict[K, int]:
    total = dict(a)
    for key, value in b.items():
        total[key] = total.get(key, 0) + value
    return total


class Briefing(_Model):
    date: dt.date
    intro: str
    stories: list[Story]
    light_day: bool
    stats: RunStats


class ReviewDecision(_Model):
    action: Literal["approve", "edit", "reject"]
    edited_stories: list[Story] = Field(default_factory=list[Story])
    removed_story_ids: list[str] = Field(default_factory=list[str])
    decided_by: Literal["human", "timeout", "auto"]
