import ssl
from typing import Any, Self

import pytest

from context_window import embeddings
from context_window.embeddings import cosine, make_embedder
from tests.fakes import EMBED_DIM, settings


class _Response:
    def __init__(self, count: int) -> None:
        self.count = count

    def model_dump(self) -> dict[str, Any]:
        rows = [
            {"index": i, "embedding": [3.0, 4.0] + [0.0] * (EMBED_DIM - 2)}
            for i in range(self.count)
        ]
        return {"data": rows[::-1], "usage": {"prompt_tokens": 10, "cost": 0.0000002}}


class _FlakyClient:
    """Fails the first request with a dropped TLS record, then answers."""

    attempts = 0

    def __init__(self, api_key: str) -> None:
        self.embeddings = self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def generate_async(self, *, model: str, input: list[str], dimensions: int) -> _Response:
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise ssl.SSLError("bad record mac")
        return _Response(len(input))


async def test_embeddings_retry_network_errors_and_come_back_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings, "OpenRouter", _FlakyClient)
    monkeypatch.setattr(embeddings, "BACKOFF_SECONDS", 0.0)
    embed = make_embedder(settings(model_api_key="sk-test"))
    vectors = await embed(["a", "b"])
    assert _FlakyClient.attempts == 2
    assert len(vectors) == 2 and vectors[0][:2] == pytest.approx([0.6, 0.8])
    assert cosine(vectors[0], vectors[1]) == pytest.approx(1.0)
