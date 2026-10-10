"""The briefing date, shared by the runner and the API (which must stay light to import)."""

import datetime as dt
from zoneinfo import ZoneInfo

from context_window.config import Settings


def run_date_for(settings: Settings, now: dt.datetime | None = None) -> dt.date:
    """The briefing date: today in BRIEFING_TZ."""
    moment = now or dt.datetime.now(dt.UTC)
    return moment.astimezone(ZoneInfo(settings.briefing_tz)).date()
