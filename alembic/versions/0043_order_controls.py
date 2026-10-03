"""Order controls: favorite, sealed, who decided open/closed, expected date.

Adds is_favorite, is_sealed, open_set_by and expected_until to orders. A
written order that is open gets its text marked stale and a write job
queued, so the model fills expected_until for it on the next pass; the
automatic close skips stale orders, so nothing closes before that rewrite
has landed.

Revision ID: 0043_order_controls
Revises: 0042_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0043_order_controls"
down_revision: str | None = "0042_webhooks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "orders", sa.Column("is_sealed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "orders", sa.Column("open_set_by", sa.Text(), nullable=False, server_default="ai"),
    )
    op.add_column("orders", sa.Column("expected_until", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_orders_open_set_by", "orders", "open_set_by IN ('ai', 'user', 'auto')",
    )

    op.execute(
        "UPDATE orders SET text_stale = true, updated_at = now() "
        "WHERE written_at IS NOT NULL AND is_open"
    )
    op.execute(
        """
        INSERT INTO order_jobs (kind, order_id, origin, priority)
        SELECT 'write', id, 'manual', 50
        FROM orders
        WHERE written_at IS NOT NULL AND is_open
        ON CONFLICT (order_id) WHERE kind = 'write' AND status = 'pending' DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_open_set_by", "orders", type_="check")
    op.drop_column("orders", "expected_until")
    op.drop_column("orders", "open_set_by")
    op.drop_column("orders", "is_sealed")
    op.drop_column("orders", "is_favorite")
