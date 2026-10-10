"""Starting newsroom jobs through GitHub's workflow_dispatch (PLAN.md 2 and 8).

The API never runs the graph itself: it asks GitHub Actions to run `cw run` or
`cw resume` with a fine-grained token that can only use Actions on this repository.
"""

from typing import Any

import httpx

from context_window.config import Settings

API = "https://api.github.com"


class DispatchError(RuntimeError):
    pass


async def dispatch(
    settings: Settings,
    inputs: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Start the newsroom workflow on the default branch with these inputs."""
    if settings.github_dispatch_token is None:
        raise DispatchError("GITHUB_DISPATCH_TOKEN is not set")
    url = (
        f"{API}/repos/{settings.github_repository}/actions/workflows/"
        f"{settings.newsroom_workflow}/dispatches"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_dispatch_token.get_secret_value()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body: dict[str, Any] = {"ref": "main", "inputs": inputs}
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.status_code != 204:
        raise DispatchError(f"GitHub refused the dispatch: HTTP {response.status_code}")
