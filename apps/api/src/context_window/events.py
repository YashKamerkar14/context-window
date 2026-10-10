"""The run's event log (`run_events`), for the live newsroom view (PLAN.md 2, 8 and 9).

The runner streams the graph with `astream(..., subgraphs=True,
stream_mode=["updates", "custom"])` and records what comes out:

- **updates**: each node's output, in two rows. The public row has only the node, its
  branch (which parallel reporter or fact-check it belongs to), counts and timings: no
  text, so nothing unpublished leaks. The admin row has the whole update.
- **custom**: progress that nodes report themselves (`progress()` below), e.g. which beat
  a reporter covers, so the web app can label the parallel branches.
- **run**: started, paused for review, finished; written by the runner.
"""

import dataclasses
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from langgraph.config import get_stream_writer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from context_window.db.models import RunEvent
from context_window.schemas.newsroom import RunStats

log = logging.getLogger(__name__)

ADMIN_PAYLOAD_CHARS = 60_000  # larger updates are summarised rather than stored whole


def progress(event: str, *, public: bool = True, **fields: Any) -> None:
    """Report progress from inside a node to the live view. A no-op when the graph is
    not being streamed (e.g. in tests)."""
    try:
        write = get_stream_writer()
    except RuntimeError:  # not inside a graph run
        return
    write({"event": event, "public": public, **fields})


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple | set):
        return [to_jsonable(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def public_summary(update: Any) -> dict[str, Any]:
    """Counts, flags and timings only: never text."""
    if not isinstance(update, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key, value in update.items():  # pyright: ignore[reportUnknownVariableType]
        name = str(key)  # pyright: ignore[reportUnknownArgumentType]
        if isinstance(value, RunStats):
            summary["timings_ms"] = value.timings_ms
            if value.errors:
                summary["errors"] = len(value.errors)
        elif isinstance(value, list | tuple):
            summary[name] = len(value)  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, bool | int | float):
            summary[name] = value
        elif value is not None:
            summary[name] = "set"
    return summary


def admin_payload(update: Any) -> dict[str, Any]:
    data = to_jsonable(update)
    text = json.dumps(data)
    if len(text) <= ADMIN_PAYLOAD_CHARS:
        return {"update": data}
    return {
        "update": None,
        "truncated": True,
        "chars": len(text),
        "summary": public_summary(update),
    }


@dataclasses.dataclass
class EventLog:
    sessions: async_sessionmaker[AsyncSession]
    run_id: uuid.UUID

    async def add(self, node: str, kind: str, payload: dict[str, Any], *, public: bool) -> None:
        await self.add_rows([(node, kind, payload, public)])

    async def add_rows(self, rows: Sequence[tuple[str, str, dict[str, Any], bool]]) -> None:
        """Insert events in one round trip; committed at once, so polling sees them live."""
        try:
            async with self.sessions() as session:
                for node, kind, payload, public in rows:
                    session.add(
                        RunEvent(
                            run_id=self.run_id, node=node, kind=kind, public=public, payload=payload
                        )
                    )
                await session.commit()
        except Exception:  # the live view is not worth failing the run for
            log.warning("could not record a run event", exc_info=True)

    async def record(self, namespace: Sequence[str], mode: str, data: Any) -> None:
        """Record one chunk of `astream(subgraphs=True, stream_mode=[...])` output."""
        branch = namespace[0] if namespace else None
        if mode == "custom" and isinstance(data, Mapping):
            event: dict[str, Any] = dict(cast(Mapping[str, Any], data))
            public = bool(event.pop("public", True))
            name = str(event.pop("event", "progress"))
            await self.add(
                name, "progress", {"branch": branch, **to_jsonable(event)}, public=public
            )
            return
        if mode != "updates" or not isinstance(data, Mapping):
            return
        rows: list[tuple[str, str, dict[str, Any], bool]] = []
        for node, update in data.items():  # pyright: ignore[reportUnknownVariableType]
            name = str(node)  # pyright: ignore[reportUnknownArgumentType]
            if name == "__interrupt__":
                values = [getattr(i, "value", None) for i in update]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                rows.append((name, "review", {"branch": branch}, True))
                rows.append((name, "review.detail", {"request": to_jsonable(values)}, False))
                continue
            base = {"branch": branch, "node": name}
            rows.append((name, "node", {**base, **public_summary(update)}, True))
            rows.append((name, "node.detail", {**base, **admin_payload(update)}, False))
        await self.add_rows(rows)
