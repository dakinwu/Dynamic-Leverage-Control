"""Persist policy state and decision state lineage.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("strategy_evaluations", sa.Column("state_before", sa.JSON(), nullable=True))
    op.add_column("strategy_evaluations", sa.Column("state_after", sa.JSON(), nullable=True))
    op.create_table(
        "strategy_policy_state",
        sa.Column("strategy_version", sa.String(80), primary_key=True),
        sa.Column("last_advanced_as_of", sa.Date(), nullable=False, index=True),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("strategy_policy_state")
    op.drop_column("strategy_evaluations", "state_after")
    op.drop_column("strategy_evaluations", "state_before")
