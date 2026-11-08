"""Add operator state projections and paper evidence chain.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "freshness_contracts",
        sa.Column("contract_key", sa.String(160), primary_key=True),
        sa.Column("kind", sa.String(80), nullable=False, index=True),
        sa.Column("source", sa.String(255), nullable=False, index=True),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("max_age_minutes", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "automation_status",
        sa.Column("automation_id", sa.String(80), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run_id", sa.String(36), index=True),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "operator_queue_items",
        sa.Column("item_id", sa.String(120), primary_key=True),
        sa.Column("severity", sa.String(16), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("next_action", sa.Text(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(120)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(120)),
        sa.Column("reopened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "operator_queue_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("item_id", sa.String(120), nullable=False, index=True),
        sa.Column("event_type", sa.String(32), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
    )
    op.create_table(
        "manual_halt_state",
        sa.Column("halt_key", sa.String(32), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, index=True),
        sa.Column("reason", sa.Text()),
        sa.Column("actor", sa.String(120)),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_reason", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "manual_halt_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(16), nullable=False, index=True),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "strategy_selection",
        sa.Column("selection_key", sa.String(32), primary_key=True),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("changed_by", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
    )
    op.create_table(
        "paper_evidence",
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, index=True),
        sa.Column("parent_id", sa.String(36), index=True),
        sa.Column("kind", sa.String(48), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("formula_or_rule", sa.String(255)),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.execute(
        """
        CREATE TRIGGER operator_queue_events_append_only
        BEFORE UPDATE OR DELETE ON operator_queue_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER manual_halt_events_append_only
        BEFORE UPDATE OR DELETE ON manual_halt_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER paper_evidence_append_only
        BEFORE UPDATE OR DELETE ON paper_evidence
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )


def downgrade() -> None:
    for table in (
        "paper_evidence",
        "strategy_selection",
        "manual_halt_events",
        "manual_halt_state",
        "operator_queue_events",
        "operator_queue_items",
        "automation_status",
        "freshness_contracts",
    ):
        op.drop_table(table)
