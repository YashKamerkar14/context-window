"""Text embeddings for ranking and merging, through OpenRouter's embeddings API.

Vectors are normalised to unit length, so cosine similarity is a dot product. Each call
is charged to the run's Budget (with the provider's reported cost alongside) and traced
in LangSmith as an embedding run.
"""

import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from langsmith import traceable  # pyright: ignore[reportUnknownVariableType]
from openrouter import OpenRouter

from context_window.budget import current_budget
from context_window.config import Settings

Vector = list[float]
Embedder = Callable[[Sequence[str]], Awaitable[list[Vector]]]

BATCH_SIZE = 96  # texts per request
MAX_CHARS = 4000  # about 1k tokens: headlines, summaries and abstracts fit easily


def normalise(vector: Sequence[float]) -> Vector:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two unit vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider != "openrouter":
        raise RuntimeError(f"unsupported EMBEDDING_PROVIDER: {settings.embedding_provider!r}")
    model = settings.require_embedding_model()
    api_key = settings.embedding_key()
    dimensions = settings.embedding_dim

    @traceable(run_type="embedding", name="embed")
    async def embed_batch(texts: list[str]) -> list[Vector]:
        budget = current_budget()
        if budget is not None:
            budget.start_call()
        async with OpenRouter(api_key=api_key) as client:
            response = await client.embeddings.generate_async(
                model=model, input=texts, dimensions=dimensions
            )
        if isinstance(response, str):  # the SDK's type for an unparsed body
            raise RuntimeError(f"{model} returned an unexpected response: {response[:200]}")
        data = response.model_dump()
        rows = sorted(cast(list[dict[str, Any]], data["data"]), key=lambda row: int(row["index"]))
        vectors = [normalise(cast(list[float], row["embedding"])) for row in rows]
        if any(len(v) != dimensions for v in vectors):
            raise RuntimeError(f"{model} returned vectors that are not {dimensions}-dimensional")
        usage = cast(dict[str, Any], data.get("usage") or {})
        if budget is not None:
            reported = usage.get("cost")
            budget.record(
                model,
                int(usage.get("prompt_tokens") or 0),
                0,
                float(reported) if isinstance(reported, int | float) else None,
            )
        return vectors

    async def embed(texts: Sequence[str]) -> list[Vector]:
        clipped = [" ".join(text.split())[:MAX_CHARS] or "(empty)" for text in texts]
        vectors: list[Vector] = []
        for start in range(0, len(clipped), BATCH_SIZE):
            vectors.extend(await embed_batch(clipped[start : start + BATCH_SIZE]))
        return vectors

    return embed
