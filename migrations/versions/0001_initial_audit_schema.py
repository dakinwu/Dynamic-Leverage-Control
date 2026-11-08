"""Create append-only audit schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AUDIT_TABLES = (
    "raw_snapshots",
    "strategy_evaluations",
    "risk_evaluations",
    "trade_intents",
    "broker_events",
    "paper_orders",
    "paper_fills",
)


def upgrade() -> None:
    op.create_table(
        "raw_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(80), nullable=False, index=True),
        sa.Column("source", sa.String(255), nullable=False, index=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("quality", sa.String(32), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source", "checksum", name="uq_raw_source_checksum"),
    )
    op.create_table(
        "strategy_evaluations",
        sa.Column("evaluation_id", sa.String(36), primary_key=True),
        sa.Column("strategy_version", sa.String(80), nullable=False, index=True),
        sa.Column("as_of", sa.Date(), nullable=False, index=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("raw_target", sa.Numeric(6, 3)),
        sa.Column("target_exposure", sa.Numeric(6, 3), nullable=False),
        sa.Column("safety_state", sa.String(32), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("input_snapshot_ids", sa.JSON(), nullable=False),
    )
    op.create_table(
        "risk_evaluations",
        sa.Column("evaluation_id", sa.String(36), primary_key=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("strategy_evaluation_id", sa.String(36), nullable=False, index=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("allowed_exposure", sa.Numeric(6, 3), nullable=False),
        sa.Column("safety_state", sa.String(32), nullable=False),
        sa.Column("risk_reducing_only", sa.Boolean(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
    )
    op.create_table(
        "trade_intents",
        sa.Column("intent_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("execution_recipe", sa.String(80), nullable=False),
        sa.Column("current_exposure", sa.Numeric(6, 3), nullable=False),
        sa.Column("requested_exposure", sa.Numeric(6, 3), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("maturity", sa.String(32), nullable=False),
    )
    op.create_table(
        "broker_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("broker", sa.String(32), nullable=False, index=True),
        sa.Column("broker_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("broker", "broker_event_id", name="uq_broker_event"),
    )
    op.create_table(
        "paper_orders",
        sa.Column("order_id", sa.String(36), primary_key=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("instrument", sa.String(16), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
    )
    op.create_table(
        "paper_fills",
        sa.Column("fill_id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), nullable=False, index=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("instrument", sa.String(16), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("commission", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax", sa.Numeric(18, 2), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False, index=True),
    )

    op.execute(
        """
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit table % is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in AUDIT_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
            """
        )


def downgrade() -> None:
    for table in reversed(AUDIT_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_audit_mutation()")
