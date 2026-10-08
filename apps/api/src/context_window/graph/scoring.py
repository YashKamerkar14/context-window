"""The ranking score (PLAN.md 6.4) and the interest vectors it needs.

score = 0.40 * interest_match + 0.25 * source_authority + 0.20 * recency + 0.15 * cross_source

Every component is in [0, 1].
"""

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_window.db.models import Interest
from context_window.embeddings import Embedder, Vector, cosine
from context_window.schemas.enums import Beat

WEIGHTS = {"interest": 0.40, "authority": 0.25, "recency": 0.20, "cross_source": 0.15}
# Raw cosine from modern embedding models clusters around 0.2 to 0.6, so it is rescaled.
COSINE_FLOOR = 0.25
COSINE_SPAN = 0.45
FRESH = dt.timedelta(hours=24)
STALE = dt.timedelta(hours=72)
# Releases and research are single-source by nature (PLAN.md 6.4).
SINGLE_SOURCE_BEATS = frozenset({Beat.RELEASES, Beat.RESEARCH})
# Hosts whose links are discussion of a story, not an independent source of it.
NOT_INDEPENDENT = frozenset({"news.ycombinator.com"})


@dataclass(frozen=True)
class InterestVector:
    topic: str
    weight: float
    vector: Vector


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def interest_match(vector: Sequence[float], interests: Iterable[InterestVector]) -> float:
    return max(
        (
            clamp((cosine(vector, i.vector) - COSINE_FLOOR) / COSINE_SPAN) * (i.weight / 2.0)
            for i in interests
        ),
        default=0.0,
    )


def authority_score(best_authority: int) -> float:
    return clamp((best_authority - 1) / 4)


def recency(published_at: dt.datetime | None, run_start: dt.datetime) -> float:
    """1.0 within 24 hours of the run's start, falling linearly to 0 at 72 hours. An
    unknown date counts as the middle of that range."""
    if published_at is None:
        return 0.5
    age = run_start - published_at
    if age <= FRESH:
        return 1.0
    return clamp(1 - (age - FRESH) / (STALE - FRESH))


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    # Registrable domain, approximately: the last two labels (blog.google -> blog.google,
    # openai.com -> openai.com, research.google -> research.google).
    return ".".join(host.split(".")[-2:])


def independent_domains(urls: Iterable[str]) -> int:
    return len(
        {
            _domain(url)
            for url in urls
            if (urlsplit(url).hostname or "").lower().removeprefix("www.") not in NOT_INDEPENDENT
        }
    )


def cross_source(beat: Beat, urls: Iterable[str]) -> float:
    if beat in SINGLE_SOURCE_BEATS:
        return 0.5
    return clamp((min(independent_domains(urls), 3) - 1) / 2)


def score(*, interest: float, authority: float, fresh: float, cross: float) -> float:
    return round(
        WEIGHTS["interest"] * interest
        + WEIGHTS["authority"] * authority
        + WEIGHTS["recency"] * fresh
        + WEIGHTS["cross_source"] * cross,
        4,
    )


async def interest_vectors(
    session: AsyncSession, embed: Embedder, model: str
) -> list[InterestVector]:
    """Every interest with an up-to-date embedding, embedding any that lack one. The
    caller commits."""
    interests = (await session.scalars(select(Interest).order_by(Interest.id))).all()
    stale = [i for i in interests if i.embedding is None or i.embedding_model != model]
    if stale:
        vectors = await embed([f"{i.topic}: {i.description or i.topic}" for i in stale])
        for interest, vector in zip(stale, vectors, strict=True):
            interest.embedding = vector
            interest.embedding_model = model
        await session.flush()
    return [
        InterestVector(i.topic, i.weight, [float(x) for x in i.embedding])
        for i in interests
        if i.embedding is not None
    ]
