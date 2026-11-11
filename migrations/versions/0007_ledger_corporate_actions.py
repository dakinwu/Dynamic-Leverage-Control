"""Track dividend receivables and unconfirmed dividend payments on the strategy ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("receivables", "unconfirmed_dividends"):
        op.add_column(
            "strategy_ledger_state",
            sa.Column(column, sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade():
    for column in ("unconfirmed_dividends", "receivables"):
        op.drop_column("strategy_ledger_state", column)
