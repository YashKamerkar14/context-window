from typing import Any

from context_window.evaluation import (
    covered,
    duplicates,
    published_stories,
    quote_on_page,
    sample_claims,
    unverified_sentences,
)


def _story(
    story_id: str,
    *,
    sentences: list[tuple[str, list[str]]],
    verdicts: dict[str, str],
    keys: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": story_id,
        "headline": f"Headline {story_id}",
        "summary": " ".join(s for s, _ in sentences),
        "sentence_claim_map": [{"sentence": s, "claim_ids": ids} for s, ids in sentences],
        "claims": [
            {"id": cid, "text": f"claim {cid}", "source_url": f"https://e.com/{cid}", "verdict": v}
            for cid, v in verdicts.items()
        ],
        "canonical_keys": keys or [],
    }


def _briefing(date: str, *stories: dict[str, Any]) -> dict[str, Any]:
    return {"date": date, "stories": list(stories)}


def test_a_sentence_counts_as_verified_only_with_a_supported_claim() -> None:
    stories = published_stories(
        [
            _briefing(
                "2026-10-10",
                _story(
                    "a",
                    sentences=[("One.", ["c1"]), ("Two.", ["c2"]), ("Three.", [])],
                    verdicts={"c1": "supported", "c2": "unsupported"},
                ),
            )
        ]
    )
    rate, examples = unverified_sentences(stories)
    assert (rate.hits, rate.total) == (2, 3)
    assert [e.rsplit(": ", 1)[1] for e in examples] == ["Two.", "Three."]


def test_a_repeat_is_a_shared_item_or_a_close_embedding_within_the_window() -> None:
    one = _story("a", sentences=[("x", [])], verdicts={}, keys=["gh:o/r@1"])
    two = _story("b", sentences=[("x", [])], verdicts={}, keys=["gh:o/r@1"])
    three = _story("c", sentences=[("x", [])], verdicts={})
    four = _story("d", sentences=[("x", [])], verdicts={})
    far = _story("e", sentences=[("x", [])], verdicts={})
    stories = published_stories(
        [
            _briefing("2026-10-01", one, three),
            _briefing("2026-10-02", two, four),
            _briefing("2026-10-30", far),
        ]
    )
    vectors = {"a": [1, 0], "b": [0, 1], "c": [0, 1], "d": [0, 1], "e": [0, 1]}
    found = duplicates(stories, vectors, threshold=0.8)
    assert [(d.earlier.id, d.later.id) for d in found] == [("a", "b"), ("c", "d")]
    assert found[0].reason == "same item gh:o/r@1"
    # Same edition, or more than 14 days apart, is not a repeat.
    assert all(d.later.id != "e" for d in found)


def test_the_claim_sample_is_fixed_and_only_takes_supported_claims() -> None:
    story = _story(
        "a",
        sentences=[("x", [])],
        verdicts={f"c{i}": "supported" for i in range(10)} | {"bad": "unsupported"},
    )
    stories = published_stories([_briefing("2026-10-10", story)])
    first = sample_claims(stories, 4)
    assert first == sample_claims(stories, 4)
    assert len(first) == 4 and all(not c["key"].endswith(":bad") for c in first)
    assert len(sample_claims(stories, 50)) == 10


def test_an_evidence_quote_must_appear_in_the_page() -> None:
    page = "<html><body><p>Models are upgraded &amp; kept\n in the background.</p></body>"
    assert quote_on_page("models are upgraded & kept in the background", page)
    assert quote_on_page("Models are upgraded ... in the background.", page)
    assert not quote_on_page("Models are deleted in the background", page)
    # Links leave spaces when tags are removed; abstracts keep LaTeX the page renders.
    release = 'retry on overflow (<a href="/pull/41159">#41159</a>)'
    assert quote_on_page("retry on overflow (#41159)", release)
    abstract = "clears every level of all \\(25\\) public games, 59" + chr(0x2013) + "82% fewer"
    assert quote_on_page("clears every level of all 25 public games", abstract)
    assert quote_on_page(
        "all \\(25\\) public games, \\(59\\)" + chr(0xFFFD) + "82% fewer", abstract
    )


def test_a_listed_story_is_covered_by_keywords_or_a_close_embedding() -> None:
    stories = published_stories(
        [
            _briefing(
                "2026-10-10",
                _story("a", sentences=[("Ollama 0.40 ships", [])], verdicts={}),
                _story("b", sentences=[("Something else", [])], verdicts={}),
            )
        ]
    )
    vectors = {"a": [1, 0], "b": [0, 1]}
    assert covered([0, 1], stories, vectors, threshold=0.7) is stories[1]
    hit = covered([0.6, 0.8], stories, vectors, threshold=0.99, keywords=["ollama", "0.40"])
    assert hit is stories[0]
    assert covered([0.6, 0.8], stories, vectors, threshold=0.99) is None
