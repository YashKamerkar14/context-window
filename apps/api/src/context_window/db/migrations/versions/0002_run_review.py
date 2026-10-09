"""run review: when a run started waiting, and the decision it got

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("review_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "runs",
        sa.Column("review_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "review_decision")
    op.drop_column("runs", "review_requested_at")
