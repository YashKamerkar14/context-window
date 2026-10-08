"""Which database objects belong to our migrations.

LangGraph's checkpointer creates and migrates its own `checkpoint*` tables, and Alembic
tracks itself in `alembic_version`. Autogenerate must never try to create or drop them.
"""

from typing import Any

CHECKPOINTER_PREFIX = "checkpoint"
ALEMBIC_VERSION_TABLE = "alembic_version"


def _foreign(table_name: str) -> bool:
    return table_name.startswith(CHECKPOINTER_PREFIX) or table_name == ALEMBIC_VERSION_TABLE


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table":
        return name is None or not _foreign(name)
    table = getattr(obj, "table", None)
    return table is None or not _foreign(table.name)
