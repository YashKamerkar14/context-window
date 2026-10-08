"""A typed wrapper around feedparser, which ships without type information."""

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Any, cast

import feedparser  # pyright: ignore[reportMissingTypeStubs]


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str | None
    summary: str
    published_at: dt.datetime | None
    guid: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ParsedFeed:
    title: str | None
    entries: list[FeedEntry]
    is_feed: bool
    problem: str | None


def _time(entry: dict[str, Any], *keys: str) -> dt.datetime | None:
    for key in keys:
        value = entry.get(key)
        if value:
            return dt.datetime.fromtimestamp(calendar.timegm(value), tz=dt.UTC)
    return None


def parse_feed(content: bytes) -> ParsedFeed:
    parsed = cast(Any, feedparser).parse(content)
    feed = cast(dict[str, Any], parsed.get("feed", {}))
    raw_entries = cast(list[dict[str, Any]], parsed.get("entries", []))
    version = cast(str, parsed.get("version", ""))
    bozo = bool(parsed.get("bozo"))
    problem = str(parsed.get("bozo_exception")) if bozo else None

    entries = [
        FeedEntry(
            title=str(entry.get("title", "")).strip(),
            link=cast(str | None, entry.get("link")),
            summary=str(entry.get("summary", "")),
            published_at=_time(entry, "published_parsed", "updated_parsed"),
            guid=cast(str | None, entry.get("id")),
            raw=entry,
        )
        for entry in raw_entries
    ]
    # feedparser is lenient: a feed with minor problems still has a version and entries.
    is_feed = bool(version) or bool(entries)
    return ParsedFeed(
        title=cast(str | None, feed.get("title")), entries=entries, is_feed=is_feed, problem=problem
    )
