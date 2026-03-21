"""Add image_exceptions table, folder management columns.

Adds:
- image_exceptions table (per-account sender/domain allowlist for remote images)
- folder_order JSONB and idle_folders JSONB on accounts
- is_visible boolean on folders

Revision ID: 005
Revises: 004
Create Date: 2026-03-21

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: Union[str, Sequence[str], None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add image_exceptions table and folder management columns."""
    # Image exceptions table
    op.create_table(
        "image_exceptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("exception_type", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id", "exception_type", "value",
            name="uq_image_exception",
        ),
    )
    op.create_index(
        "idx_image_exception_account",
        "image_exceptions",
        ["account_id"],
    )

    # Folder management columns on accounts
    op.add_column(
        "accounts",
        sa.Column("folder_order", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("idle_folders", sa.dialects.postgresql.JSONB(), nullable=True),
    )

    # Visibility flag on folders
    op.add_column(
        "folders",
        sa.Column(
            "is_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    """Remove image_exceptions table and folder management columns."""
    op.drop_column("folders", "is_visible")
    op.drop_column("accounts", "idle_folders")
    op.drop_column("accounts", "folder_order")
    op.drop_index("idx_image_exception_account", table_name="image_exceptions")
    op.drop_table("image_exceptions")
