"""Context Window: a multi-agent AI newsroom."""

import os

# LangGraph reads this once, when its serializer is first imported, and only from the
# process environment. Strict mode restricts checkpoint loading to an allowlist of types
# (PLAN.md 6.5), so it is switched on here, before any module can import LangGraph.
# Setting LANGGRAPH_STRICT_MSGPACK=false in the real environment still turns it off.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

__version__ = "0.1.0"
