"""Persist operation transitions and retryable notifications outside GET requests."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operations_projection_state",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("channel", sa.String(180), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("notification_outbox")
    op.drop_table("operations_projection_state")
