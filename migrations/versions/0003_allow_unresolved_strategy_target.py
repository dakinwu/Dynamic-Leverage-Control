"""Allow invalid strategy runs to explicitly have no target.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("strategy_evaluations", "target_exposure", nullable=True)


def downgrade() -> None:
    # Existing invalid runs may contain NULL after this revision, so a
    # downgrade must be explicit rather than silently inventing a target.
    op.execute(
        "UPDATE strategy_evaluations SET target_exposure = 1.0 WHERE target_exposure IS NULL"
    )
    op.alter_column("strategy_evaluations", "target_exposure", nullable=False)
