"""LangSmith tracing.

LangChain and LangGraph read LangSmith settings only from the process environment, and
`.env` is read by our settings, not exported. So `configure_tracing` copies them over
before a run. Only `cw run` calls it: tests and other commands never trace.

A run's root trace has the run's id, so its URL can be looked up afterwards and stored
in `runs.trace_url`.
"""

import asyncio
import logging
import os
import uuid

from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith import Client

from context_window.config import Settings

log = logging.getLogger(__name__)


def configure_tracing(settings: Settings) -> bool:
    """Turn tracing on (with a key and LANGSMITH_TRACING) or off. Returns whether it is on."""
    if settings.langsmith_api_key is None or not settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "false"
        return False
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true"
    return True


def flush_traces() -> None:
    """Block until every pending trace is sent; the process may exit right after a run."""
    wait_for_all_tracers()


async def trace_url(settings: Settings, run_id: uuid.UUID) -> str | None:
    """The LangSmith URL of the run's trace, or None if it cannot be found."""
    if settings.langsmith_api_key is None:
        return None
    try:
        client = Client(api_key=settings.langsmith_api_key.get_secret_value())
        # Both make blocking requests: the project lookup, and the server version check
        # that `client.runs` performs on first use.
        project = await asyncio.to_thread(
            client.read_project, project_name=settings.langsmith_project
        )
        runs = await asyncio.to_thread(lambda: client.runs)
        response = await runs.get_url(str(run_id), project_id=str(project.id), trace_id=str(run_id))
        return response.url
    except Exception:  # a missing link is not worth failing the run for
        log.warning("could not look up the LangSmith trace URL", exc_info=True)
        return None
