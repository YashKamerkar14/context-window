"""Vector maths shared by the newsroom and the API (which must stay light to import)."""

import math
from collections.abc import Sequence

Vector = list[float]


def normalise(vector: Sequence[float]) -> Vector:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two unit vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))
