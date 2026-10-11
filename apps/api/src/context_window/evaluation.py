"""The metrics in PLAN.md 10, as pure functions over published briefings.

`evals/run_evals.py` fetches the briefings from the API, adds embeddings and page text,
and prints the table. Everything here is deterministic so it can be tested.
"""

import datetime as dt
import html
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from context_window.vectors import cosine


@dataclass(frozen=True)
class Published:
    """One published story, as the API returns it, with its edition's date."""

    date: dt.date
    story: Mapping[str, Any]

    @property
    def id(self) -> str:
        return str(self.story["id"])

    @property
    def headline(self) -> str:
        return str(self.story["headline"])

    def text(self) -> str:
        return f"{self.story['headline']}\n{self.story['summary']}"


def published_stories(briefings: Sequence[Mapping[str, Any]]) -> list[Published]:
    return [
        Published(dt.date.fromisoformat(b["date"]), story)
        for b in sorted(briefings, key=lambda b: b["date"])
        for story in b["stories"]
    ]


@dataclass(frozen=True)
class Rate:
    hits: int
    total: int

    @property
    def share(self) -> float | None:
        return self.hits / self.total if self.total else None


def unverified_sentences(stories: Sequence[Published]) -> tuple[Rate, list[str]]:
    """Summary sentences with no supported claim behind them (headlines and "why it
    matters" are not mapped to claims, so they are outside this measure)."""
    unverified: list[str] = []
    total = 0
    for item in stories:
        supported = {c["id"] for c in item.story["claims"] if c.get("verdict") == "supported"}
        entries: list[Mapping[str, Any]] = item.story.get("sentence_claim_map") or []
        for entry in entries:
            total += 1
            claim_ids: list[str] = entry.get("claim_ids") or []
            if not supported.intersection(claim_ids):
                unverified.append(f"{item.date} {item.headline}: {entry['sentence']}")
    return Rate(len(unverified), total), unverified


@dataclass(frozen=True)
class Duplicate:
    earlier: Published
    later: Published
    reason: str


def duplicates(
    stories: Sequence[Published],
    vectors: Mapping[str, Sequence[float]],
    *,
    threshold: float,
    window_days: int = 14,
) -> list[Duplicate]:
    """Later stories that repeat an earlier edition's story within the window: the same
    source item (canonical key), or embeddings at least `threshold` similar."""
    found: list[Duplicate] = []
    for i, later in enumerate(stories):
        later_keys = set(later.story.get("canonical_keys") or [])
        for earlier in stories[:i]:
            gap = (later.date - earlier.date).days
            if gap <= 0 or gap > window_days:
                continue  # the same edition is the desk's job, not a repeat
            shared = later_keys & set(earlier.story.get("canonical_keys") or [])
            if shared:
                found.append(Duplicate(earlier, later, f"same item {sorted(shared)[0]}"))
                break
            similarity = cosine(vectors[earlier.id], vectors[later.id])
            if similarity >= threshold:
                found.append(Duplicate(earlier, later, f"similarity {similarity:.2f}"))
                break
    return found


def sample_claims(
    stories: Sequence[Published], size: int, *, seed: int = 10
) -> list[dict[str, Any]]:
    """A fixed random sample of published claims to hand-check (seeded, so a re-run
    with more editions keeps the earlier picks where it can)."""
    claims = [
        {
            "key": f"{item.id}:{claim['id']}",
            "date": item.date.isoformat(),
            "headline": item.headline,
            "claim": claim["text"],
            "evidence_url": claim.get("evidence_url") or claim["source_url"],
            "evidence_quote": claim.get("evidence_quote"),
            "evidence_kind": claim.get("evidence_kind"),
        }
        for item in stories
        for claim in item.story["claims"]
        if claim.get("verdict") == "supported"
    ]
    rng = random.Random(seed)
    return sorted(rng.sample(claims, min(size, len(claims))), key=lambda c: c["key"])


_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
# Curly quotes and dashes to ASCII (code points, since the characters look alike).
_QUOTES = str.maketrans(
    {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"'}
    | {chr(c): "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)}  # dashes
)
# Gaps in a quote: an ellipsis, or a character lost in decoding (U+FFFD, often a dash).
_GAPS = re.compile(r"\.\.\.|[" + chr(0x2026) + chr(0xFFFD) + "]")
# Inline LaTeX markup in abstracts (\( \) \! \: \, \;), which the page may render.
_LATEX = re.compile(r"\\[()!:,;]")


def plain(text: str) -> str:
    """Page or quote text reduced for comparison: no tags, entities decoded, curly quotes
    straightened, LaTeX delimiters dropped, whitespace collapsed, lower case."""
    text = html.unescape(_TAGS.sub(" ", text)).translate(_QUOTES)
    return " ".join(_LATEX.sub("", text).split()).lower()


def quote_on_page(quote: str, page: str) -> bool:
    """Whether the fact desk's evidence quote really appears in the page it cites.
    Spacing is ignored (removing tags adds spaces around links), and a quote shortened
    with an ellipsis is looked for part by part."""
    body = plain(page).replace(" ", "")
    parts = [p.strip(" .") for p in _GAPS.split(quote.translate(_QUOTES))]
    return all(plain(p).replace(" ", "") in body for p in parts if len(p.strip()) >= 8)


def covered(
    story_vector: Sequence[float],
    published: Sequence[Published],
    vectors: Mapping[str, Sequence[float]],
    *,
    threshold: float,
    keywords: Sequence[str] = (),
) -> Published | None:
    """The published story that covers a story from the hand-made list, if any: all its
    keywords appear in one story, or the embeddings are at least `threshold` similar."""
    best: tuple[float, Published] | None = None
    for item in published:
        text = item.text().lower()
        if keywords and all(k.lower() in text for k in keywords):
            return item
        similarity = cosine(story_vector, vectors[item.id])
        if similarity >= threshold and (best is None or similarity > best[0]):
            best = (similarity, item)
    return best[1] if best else None
