"""Whoever adds a file to the orders register owns it.

A file has no entry until its job has placed it, so its owner cannot be read
off an order. order_files gains a user_id: a file already in an entry takes
that entry's user, any other (still waiting, or failed) goes to the owner row
created in 0050.

Revision ID: 0051_order_files_owner
Revises: 0050_users_and_ownership
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0051_order_files_owner"
down_revision: str | None = "0050_users_and_ownership"
branch_labels: str | None = None
depends_on: str | None = None

_OWNER = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.add_column(
        "order_files", sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.execute(
        "UPDATE order_files SET user_id = orders.user_id "
        "FROM orders WHERE orders.id = order_files.order_id"
    )
    op.execute(f"UPDATE order_files SET user_id = '{_OWNER}' WHERE user_id IS NULL")
    op.create_index("ix_order_files_user_id", "order_files", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_order_files_user_id", table_name="order_files")
    op.drop_column("order_files", "user_id")
