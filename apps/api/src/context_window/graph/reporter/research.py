"""The research desk: papers from Hugging Face's daily list and from arXiv.

Hugging Face daily papers is the primary list; arXiv's newest papers in the configured
categories add the rest. Both identify a paper by its arXiv id, so a paper on both is one
lead. The arXiv full text is the primary source (PLAN.md 6.6), so a lead's URL is its
arXiv page.

arXiv lists hundreds of papers a day, so they are pre-filtered by interest embedding
before anything is saved or sent to a model (PLAN.md 2). Papers on Hugging Face's list
get a bonus in that filter, because people chose them.
"""

import datetime as dt
import uuid

from sqlalchemy import select

from context_window.db.models import Source
from context_window.graph.context import NewsroomContext
from context_window.graph.reporter.leads import (
    LOOKBACK,
    DeskResult,
    Fetched,
    Lead,
    reportable_leads,
    save_fetched,
)
from context_window.graph.reporter.releases import parse_datetime
from context_window.graph.scoring import interest_match, interest_vectors
from context_window.mcp_client import ToolCallError
from context_window.memory.canonical import arxiv_key
from context_window.schemas.enums import Beat, SourceKind
from context_window.schemas.newsroom import BeatAssignment, RunError

HF_LIMIT = 50
# arXiv answers slowly; 300 results timed out in a live run. 150 newest, plus Hugging
# Face's list, leave the interest filter plenty to choose from.
ARXIV_LIMIT = 150
HF_BONUS = 0.15
# How many papers survive the embedding filter, as a multiple of the beat's item cap.
# The rest are not saved, which keeps the items table small.
PREFILTER_FACTOR = 2


def _abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


async def research_desk(
    ctx: NewsroomContext, assignment: BeatAssignment, run_id: uuid.UUID, now: dt.datetime
) -> DeskResult:
    async with ctx.db() as session:
        sources = (
            await session.scalars(
                select(Source).where(Source.beat == Beat.RESEARCH, Source.enabled.is_(True))
            )
        ).all()
        hf = next((s for s in sources if s.kind is SourceKind.HF_PAPERS), None)
        arxiv = next((s for s in sources if s.kind is SourceKind.ARXIV), None)
        hf_ref = (hf.id, hf.authority) if hf else None
        arxiv_ref = (
            (arxiv.id, arxiv.authority, list(arxiv.options.get("categories", [])))
            if arxiv
            else None
        )

    errors: list[RunError] = []
    fetched: list[Fetched] = []
    papers: dict[str, dict[str, str]] = {}  # arxiv id -> title, abstract, hf url, upvotes

    if hf_ref is not None:
        try:
            result = await ctx.tools.call("hf_papers", "list_daily", {"limit": HF_LIMIT})
            if result.get("error"):
                errors.append(
                    RunError(node="reporter.fetch", source="hf_papers", message=result["error"])
                )
            for paper in result.get("papers", []):
                arxiv_id = str(paper["arxiv_id"])
                fetched.append(
                    Fetched(
                        source_id=hf_ref[0],
                        authority=hf_ref[1],
                        url=str(paper["url"]),
                        key=arxiv_key(arxiv_id),
                        title=str(paper["title"]),
                        published_at=parse_datetime(paper.get("published_at")),
                        excerpt=str(paper.get("summary") or ""),
                    )
                )
                papers[arxiv_id] = {
                    "title": str(paper["title"]),
                    "abstract": str(paper.get("summary") or ""),
                    "hf_url": str(paper["url"]),
                    "hf_upvotes": str(paper.get("upvotes", 0)),
                }
        except (ToolCallError, KeyError, ValueError) as exc:
            errors.append(RunError(node="reporter.fetch", source="hf_papers", message=str(exc)))

    if arxiv_ref is not None and arxiv_ref[2]:
        try:
            result = await ctx.tools.call(
                "arxiv",
                "search_recent",
                {
                    "categories": arxiv_ref[2],
                    "since": (now - LOOKBACK).isoformat(),
                    "max_results": ARXIV_LIMIT,
                },
            )
            if result.get("error"):
                errors.append(
                    RunError(node="reporter.fetch", source="arxiv", message=result["error"])
                )
            for paper in result.get("papers", []):
                arxiv_id = str(paper["arxiv_id"])
                fetched.append(
                    Fetched(
                        source_id=arxiv_ref[0],
                        authority=arxiv_ref[1],
                        url=_abs_url(arxiv_id),
                        key=arxiv_key(arxiv_id),
                        title=str(paper["title"]),
                        published_at=parse_datetime(paper.get("published_at")),
                        excerpt=str(paper.get("abstract") or ""),
                    )
                )
                entry = papers.setdefault(arxiv_id, {"title": str(paper["title"]), "abstract": ""})
                entry["abstract"] = entry["abstract"] or str(paper.get("abstract") or "")
        except (ToolCallError, KeyError, ValueError) as exc:
            errors.append(RunError(node="reporter.fetch", source="arxiv", message=str(exc)))

    # One lead per paper. Its date is the day Hugging Face featured it when it did (the
    # announcement, not the v1 submission; PLAN.md 6.4), otherwise arXiv's.
    by_id: dict[str, list[Fetched]] = {}
    for item in fetched:
        by_id.setdefault(item.key.removeprefix("arxiv:"), []).append(item)
    leads: list[Lead] = []
    for arxiv_id, items in by_id.items():
        paper = papers[arxiv_id]
        on_hf = "hf_url" in paper
        featured = [
            i.published_at for i in items if hf_ref is not None and i.source_id == hf_ref[0]
        ]
        dated = [i.published_at for i in items if i.published_at is not None]
        published = next((d for d in featured if d is not None), None) or min(dated, default=None)
        labels = {"arxiv_id": arxiv_id}
        if on_hf:
            labels["hugging_face_upvotes"] = paper["hf_upvotes"]
        leads.append(
            Lead(
                beat=Beat.RESEARCH,
                title=paper["title"],
                url=_abs_url(arxiv_id),
                source_urls=[_abs_url(arxiv_id), *([paper["hf_url"]] if on_hf else [])],
                canonical_keys=[arxiv_key(arxiv_id)],
                published_at=published,
                authority=max(i.authority for i in items),
                has_text=bool(paper["abstract"].strip()),
                labels=labels,
            )
        )

    # Pre-filter by interest before saving or writing anything.
    keep = assignment.max_items * PREFILTER_FACTOR
    async with ctx.db() as session:
        interests = await interest_vectors(
            session, ctx.embed, ctx.settings.require_embedding_model()
        )
        await session.commit()
    if leads and interests:
        texts = [f"{lead.title}. {papers[lead.labels['arxiv_id']]['abstract']}" for lead in leads]
        vectors = await ctx.embed(texts)
        ranked = sorted(
            zip(leads, vectors, strict=True),
            key=lambda pair: (
                interest_match(pair[1], interests)
                + (HF_BONUS if "hugging_face_upvotes" in pair[0].labels else 0.0)
            ),
            reverse=True,
        )
        leads = [lead for lead, _ in ranked[:keep]]
    else:
        leads = sorted(leads, key=lambda lead: "hugging_face_upvotes" not in lead.labels)[:keep]

    kept_keys = {key for lead in leads for key in lead.canonical_keys}
    async with ctx.db() as session:
        saved = await save_fetched(session, [i for i in fetched if i.key in kept_keys])
        reportable = await reportable_leads(session, leads, saved, now=now, max_items=None)
        await session.commit()
    # Keep the interest order, not the newest-first order, when capping.
    allowed = {lead.url for lead in reportable}
    leads = [lead for lead in leads if lead.url in allowed][: assignment.max_items]

    texts = {
        lead.url: f"{lead.title}\n\n{papers[lead.labels['arxiv_id']]['abstract']}"
        for lead in leads
        if lead.has_text
    }
    return DeskResult(leads=leads, texts=texts, fetched=len(fetched), errors=errors)
