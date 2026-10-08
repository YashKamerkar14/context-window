"""Event loop helpers.

psycopg's async mode cannot run on Windows' default ProactorEventLoop, so every entry point
that runs async database code goes through `run`.
"""

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro, loop_factory=loop_factory())
