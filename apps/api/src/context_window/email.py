"""The briefing email: a Jinja2 HTML template sent through Resend (PLAN.md 3 and 6.3).
Also the alert sent to the same address when a run fails (PLAN.md 12, Phase 9).

The idempotency key `briefing-<date>` makes Resend send a date's briefing at most once,
even if publishing runs twice. Story text comes from fetched sources, so the template
escapes everything.
"""

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import resend
from jinja2 import Environment, select_autoescape

from context_window.config import Settings
from context_window.schemas.newsroom import Briefing

log = logging.getLogger(__name__)

TEMPLATE = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Context Window, {{ date }}</title></head>
<body style="margin:0;padding:24px;background:#f6f6f4;font-family:Georgia,serif;color:#1d1d1b">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;padding:32px;border-radius:8px">
    <p style="margin:0;font:600 12px/1.4 Arial,sans-serif;letter-spacing:.08em;color:#6b6b66">
      CONTEXT WINDOW &middot; {{ date }}</p>
    <p style="font-size:18px;line-height:1.5;margin:12px 0 24px">{{ intro }}</p>
    {% if light_day %}
    <p style="font:14px/1.5 Arial,sans-serif;color:#6b6b66">A light day: fewer than five
      stories cleared the bar.</p>
    {% endif %}
    {% for story in stories %}
    <div style="border-top:1px solid #e4e4df;padding:20px 0">
      <p style="margin:0;font:600 11px/1.4 Arial,sans-serif;letter-spacing:.08em;color:#6b6b66">
        {{ story.beat | upper }}
        {% if story.is_breaking_change %} &middot; BREAKING CHANGE{% endif %}
        {% if story.edited_by_editor %} &middot; EDITED BY THE EDITOR{% endif %}</p>
      <h2 style="font-size:20px;line-height:1.3;margin:6px 0 8px">{{ story.headline }}</h2>
      <p style="font-size:16px;line-height:1.55;margin:0 0 8px">{{ story.summary }}</p>
      <p style="font-size:15px;line-height:1.5;margin:0 0 8px"><strong>Why it matters:</strong>
        {{ story.why_it_matters }}</p>
      <p style="font:13px/1.5 Arial,sans-serif;margin:0">
        {% for url in story.source_urls %}
        <a href="{{ url }}" style="color:#2f5d8a">{{ label(url) }}</a>
        {% if not loop.last %} &middot; {% endif %}{% endfor %}</p>
    </div>
    {% endfor %}
    <p style="font:12px/1.5 Arial,sans-serif;color:#8a8a84;
              border-top:1px solid #e4e4df;padding-top:16px">
      Every claim above was checked against its source before publication.</p>
  </div>
</body>
</html>
"""

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
_template = _env.from_string(TEMPLATE)


def _label(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def render_html(briefing: Briefing) -> str:
    return _template.render(
        date=briefing.date.isoformat(),
        intro=briefing.intro,
        light_day=briefing.light_day,
        stories=briefing.stories,
        label=_label,
    )


def render_text(briefing: Briefing) -> str:
    lines = [f"Context Window, {briefing.date.isoformat()}", "", briefing.intro, ""]
    for story in briefing.stories:
        lines += [story.headline, story.summary, f"Why it matters: {story.why_it_matters}"]
        lines += [*story.source_urls, ""]
    return "\n".join(lines)


@dataclass(frozen=True)
class EmailConfig:
    api_key: str
    sender: str
    to: str


def email_config(settings: Settings) -> EmailConfig | None:
    """None until RESEND_API_KEY, RESEND_FROM and BRIEFING_EMAIL_TO are all set."""
    if (
        settings.resend_api_key is None
        or not settings.resend_from
        or not settings.briefing_email_to
    ):
        return None
    return EmailConfig(
        settings.resend_api_key.get_secret_value(), settings.resend_from, settings.briefing_email_to
    )


def idempotency_key(briefing: Briefing) -> str:
    return f"briefing-{briefing.date.isoformat()}"


async def send_briefing(config: EmailConfig, briefing: Briefing, key: str) -> str:
    """Send the briefing; return Resend's email id."""

    def send() -> str:
        resend.api_key = config.api_key
        response = resend.Emails.send(
            {
                "from": config.sender,
                "to": [config.to],
                "subject": f"Context Window, {briefing.date.isoformat()}",
                "html": render_html(briefing),
                "text": render_text(briefing),
            },
            {"idempotency_key": key},
        )
        return str(response["id"])

    return await asyncio.to_thread(send)


def render_failure(
    *,
    run_date: dt.date,
    run_id: uuid.UUID,
    error: str,
    actions_run_url: str | None,
    newsroom_url: str | None,
) -> str:
    lines = [
        f"The newsroom run for {run_date.isoformat()} failed.",
        "",
        f"Error: {error}",
        f"Run id: {run_id}",
        "",
        "It is saved at its last checkpoint. To continue it, use the Newsroom workflow's",
        f'"Run workflow" button with command "resume", or run `cw resume {run_id}`.',
    ]
    if actions_run_url:
        lines += ["", f"Job log: {actions_run_url}"]
    if newsroom_url:
        lines += [f"Newsroom: {newsroom_url}"]
    return "\n".join(lines)


async def send_failure_alert(
    config: EmailConfig,
    *,
    run_date: dt.date,
    run_id: uuid.UUID,
    error: str,
    actions_run_url: str | None,
    newsroom_url: str | None,
) -> str:
    """Tell the editor a run failed; return Resend's email id."""
    text = render_failure(
        run_date=run_date,
        run_id=run_id,
        error=error,
        actions_run_url=actions_run_url,
        newsroom_url=newsroom_url,
    )

    def send() -> str:
        resend.api_key = config.api_key
        response = resend.Emails.send(
            {
                "from": config.sender,
                "to": [config.to],
                "subject": f"Context Window: the run for {run_date.isoformat()} failed",
                "text": text,
            }
        )
        return str(response["id"])

    return await asyncio.to_thread(send)
