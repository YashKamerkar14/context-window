"""Evidence for claims: the primary source's text, trimmed to the passages that matter.

- **Which page.** A claim's `source_url` is its primary source (PLAN.md 6.6). For an arXiv
  paper that is the full text, so `abs/` links are fetched as arXiv's HTML version.
- **Fetch once.** Pages go through the run's fetch cache, so a page cited by several
  claims, or already fetched by a reporter, is fetched once per run.
- **Fallback.** If the page is blocked by robots.txt, missing, or has no main text, the
  check uses the text the reporter wrote from (release notes, a feed summary or an
  abstract) and records `evidence_kind=feed`.
- **Trim.** The text is cut into overlapping passages; the ones most similar to the
  claims, up to about 3k tokens, are kept in page order.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from context_window.embeddings import Embedder, cosine
from context_window.fetching import fetch_page_cached, load_feed_texts
from context_window.mcp_client import ToolCallError
from context_window.schemas.enums import EvidenceKind

if TYPE_CHECKING:  # the context imports the fact-checker's types
    from context_window.graph.context import NewsroomContext

_ARXIV_ABS = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/(?P<id>[^?#]+)$")
PASSAGE_CHARS = 1200
PASSAGE_OVERLAP = 200
TRIM_CHARS = 12_000  # about 3k tokens (PLAN.md 6.3)


def fetch_url(source_url: str) -> str:
    """The URL to fetch for a claim's source: arXiv abstracts become the full text."""
    match = _ARXIV_ABS.match(source_url)
    if match:
        return f"https://arxiv.org/html/{match.group('id')}"
    return source_url


@dataclass(frozen=True)
class Evidence:
    url: str  # the source URL the claims cite
    kind: EvidenceKind
    text: str  # the full text the quote check searches
    problem: str | None = None  # why the page itself could not be used, if it could not


async def gather_evidence(
    ctx: "NewsroomContext", run_id: uuid.UUID, source_url: str
) -> Evidence | None:
    """The page's main text, or the reporter's text as a fallback, or None if neither."""
    problem: str | None = None
    try:
        async with ctx.db() as session:
            page = await fetch_page_cached(session, ctx.tools, run_id, fetch_url(source_url))
            await session.commit()
        if page.status == "ok" and page.text.strip():
            return Evidence(source_url, EvidenceKind.PAGE, page.text)
        problem = page.error or page.status
    except ToolCallError as exc:
        problem = str(exc)
    async with ctx.db() as session:
        feed = (await load_feed_texts(session, run_id, [source_url])).get(source_url, "")
    if feed.strip():
        return Evidence(source_url, EvidenceKind.FEED, feed, problem)
    return None


def passages(text: str, size: int = PASSAGE_CHARS, overlap: int = PASSAGE_OVERLAP) -> list[str]:
    """Overlapping windows of the text, broken at whitespace where possible."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            space = text.rfind(" ", start + size // 2, end)
            end = space if space > start else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


async def trim(text: str, claims: Sequence[str], embed: Embedder, budget: int = TRIM_CHARS) -> str:
    """The passages most relevant to any of the claims, up to `budget` characters, in the
    order they appear on the page."""
    if len(text) <= budget:
        return text
    chunks = passages(text)
    vectors = await embed([*claims, *chunks])
    claim_vectors, chunk_vectors = vectors[: len(claims)], vectors[len(claims) :]
    relevance = [max(cosine(c, v) for c in claim_vectors) for v in chunk_vectors]
    chosen: list[int] = []
    used = 0
    for index in sorted(range(len(chunks)), key=lambda i: relevance[i], reverse=True):
        if used + len(chunks[index]) > budget:
            continue
        chosen.append(index)
        used += len(chunks[index])
    return "\n[...]\n".join(chunks[i] for i in sorted(chosen))


_QUOTES = str.maketrans(
    {
        chr(0x2018): "'",  # left single quotation mark
        chr(0x2019): "'",  # right single quotation mark
        chr(0x201C): '"',  # left double quotation mark
        chr(0x201D): '"',  # right double quotation mark
        chr(0x2013): "-",  # en dash
        chr(0x2014): "-",  # em dash
        chr(0x00A0): " ",  # no-break space
    }
)


def normalise(text: str) -> str:
    """For the quote check: case, whitespace, curly quotes and dashes don't matter."""
    return " ".join(text.translate(_QUOTES).lower().split())


def quote_found(quote: str, text: str) -> bool:
    """True when the evidence quote appears in the source text (PLAN.md 6.3)."""
    needle = normalise(quote)
    return len(needle) >= 8 and needle in normalise(text)
