"""Every row of a table a user owns names its user.

0050 added user_id to the tables whose rows belong to one user, nullable
until every writer stamped it; every writer now does. So the column becomes
NOT NULL on push_subscriptions, unified_views, orders, order_files,
pipeline_revisions, message_action_submissions and outbox_submissions. alerts
and llm_usage keep it nullable: an alert about the instance and a model call
made for no account belong to nobody.

A row written without a user before this revision is given one first: an
order takes the owner of the accounts its mail came from, a file its order's
user, and anything else the owner row, which every row belonged to before
there were users. A unified view given to the owner that way keeps its name
unless the owner already has a view by that name; then it gains a suffix.

Revision ID: 0053_user_id_not_null
Revises: 0052_folder_pipeline_switch
"""

from __future__ import annotations

from alembic import op

revision: str = "0053_user_id_not_null"
down_revision: str | None = "0052_folder_pipeline_switch"
branch_labels: str | None = None
depends_on: str | None = None

_OWNER = "00000000-0000-0000-0000-000000000001"

# In backfill order: order_files reads orders' user, so orders come first.
_TABLES = (
    "orders",
    "order_files",
    "push_subscriptions",
    "unified_views",
    "pipeline_revisions",
    "message_action_submissions",
    "outbox_submissions",
)


def upgrade() -> None:
    op.execute(
        """
        UPDATE orders SET user_id = owners.user_id
        FROM (
            SELECT DISTINCT ON (m.order_id) m.order_id, o.user_id
            FROM order_mails m JOIN account_owners o ON o.account_id = m.account_id
            ORDER BY m.order_id, m.created_at
        ) AS owners
        WHERE orders.user_id IS NULL AND owners.order_id = orders.id
        """
    )
    op.execute(
        "UPDATE order_files SET user_id = orders.user_id FROM orders "
        "WHERE order_files.user_id IS NULL AND orders.id = order_files.order_id"
    )
    op.execute(
        f"""
        UPDATE unified_views v SET name = v.name || ' (' || left(v.id::text, 8) || ')'
        WHERE v.user_id IS NULL AND EXISTS (
            SELECT 1 FROM unified_views mine
            WHERE mine.user_id = '{_OWNER}' AND mine.name = v.name
        )
        """
    )
    for table in _TABLES:
        op.execute(f"UPDATE {table} SET user_id = '{_OWNER}' WHERE user_id IS NULL")
        op.alter_column(table, "user_id", nullable=False)


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "user_id", nullable=True)
