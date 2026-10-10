"""Reader feedback on a story (PLAN.md 6.4 and 8). Admin only: there is one reader.

One vote per story; voting again replaces it. A vote nudges the weights of the two
interests most similar to the story (cosine at least 0.30):

    w <- clamp(w + 0.1 * vote * cos, 0.2, 2.0)

A changed vote first reverses the previous nudge, so votes never pile up. With one
reader the signal is weak, so the nudge is deliberately small.
"""

import uuid
from collections.abc import Sequence
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from context_window.api.deps import AdminOnly, SessionDep
from context_window.db import models as db
from context_window.vectors import cosine, normalise

router = APIRouter(prefix="/stories", tags=["stories"])

NUDGE = 0.1
MIN_COSINE = 0.30
TOP_INTERESTS = 2
MIN_WEIGHT, MAX_WEIGHT = 0.2, 2.0


class FeedbackIn(BaseModel):
    value: Literal[1, -1]


class InterestChange(BaseModel):
    topic: str
    weight: float


class FeedbackOut(BaseModel):
    story_id: str
    value: int
    interests: list[InterestChange]


def nudge(
    interests: Sequence[db.Interest], story_vector: Sequence[float], vote: int
) -> list[db.Interest]:
    """Apply one vote's nudge to the closest interests; return the ones changed."""
    story = normalise(story_vector)
    scored = [
        (cosine(story, normalise(list(i.embedding))), i)
        for i in interests
        if i.embedding is not None
    ]
    closest = sorted((s for s in scored if s[0] >= MIN_COSINE), key=lambda s: -s[0])
    changed: list[db.Interest] = []
    for similarity, interest in closest[:TOP_INTERESTS]:
        weight = interest.weight + NUDGE * vote * similarity
        interest.weight = round(min(MAX_WEIGHT, max(MIN_WEIGHT, weight)), 4)
        changed.append(interest)
    return changed


@router.post("/{story_id}/feedback", dependencies=[AdminOnly])
async def feedback(story_id: uuid.UUID, body: FeedbackIn, session: SessionDep) -> FeedbackOut:
    story = await session.get(db.Story, story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no story {story_id}")
    previous = await session.scalar(
        select(db.Feedback.value).where(db.Feedback.story_id == story_id)
    )

    changed: dict[str, db.Interest] = {}
    if previous != body.value and story.embedding is not None:
        interests = (await session.scalars(select(db.Interest))).all()
        vector = [float(x) for x in story.embedding]
        if previous is not None:
            changed.update({i.topic: i for i in nudge(interests, vector, -previous)})
        changed.update({i.topic: i for i in nudge(interests, vector, body.value)})

    stmt = insert(db.Feedback).values(story_id=story_id, value=body.value)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[db.Feedback.story_id], set_={"value": stmt.excluded.value}
        )
    )
    await session.commit()
    return FeedbackOut(
        story_id=str(story_id),
        value=body.value,
        interests=[InterestChange(topic=i.topic, weight=i.weight) for i in changed.values()],
    )
