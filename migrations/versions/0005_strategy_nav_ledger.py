"""Add the strategy-only NAV ledger and immutable event trail.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_ledger_state",
        sa.Column("ledger_key", sa.String(32), primary_key=True),
        sa.Column("capital_budget", sa.Numeric(18, 2), nullable=False),
        sa.Column("net_contributions", sa.Numeric(18, 2), nullable=False),
        sa.Column("cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("positions", sa.JSON(), nullable=False),
        sa.Column("last_prices", sa.JSON(), nullable=False),
        sa.Column("current_nav", sa.Numeric(18, 2), nullable=False),
        sa.Column("peak_nav", sa.Numeric(18, 2), nullable=False),
        sa.Column("principal_loss_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("principal_loss_ratio", sa.Numeric(18, 8), nullable=False),
        sa.Column("current_drawdown", sa.Numeric(18, 8), nullable=False),
        sa.Column("maximum_drawdown", sa.Numeric(18, 8), nullable=False),
        sa.Column("position_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconciled", sa.Boolean(), nullable=False, index=True),
        sa.Column("reconciliation_reason", sa.Text()),
        sa.Column("last_account_snapshot_id", sa.String(36), index=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("actual_exposure", sa.Numeric(18, 8)),
        sa.Column("account_exposure", sa.Numeric(18, 8)),
        sa.Column("exposure_quality", sa.String(64), nullable=False),
    )
    op.create_table(
        "strategy_ledger_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_key", sa.String(180), nullable=False, unique=True, index=True),
        sa.Column("event_type", sa.String(32), nullable=False, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("account_snapshot_id", sa.String(36), index=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.execute(
        """
        CREATE TRIGGER strategy_ledger_events_append_only
        BEFORE UPDATE OR DELETE ON strategy_ledger_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )


def downgrade() -> None:
    op.drop_table("strategy_ledger_events")
    op.drop_table("strategy_ledger_state")
