"""Add settings, job_states tables and account v2 columns.

Revision ID: 002
Revises: 001
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "002"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add v2 schema changes."""
    # settings table
    op.create_table(
        "settings",
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("data", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("category"),
    )

    # job_states table
    op.create_table(
        "job_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("cursor", JSONB(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("name", "account_id", name="uq_job_state_name_account"),
    )
    op.create_index("idx_job_state_name", "job_states", ["name"])

    # Account v2 columns
    op.add_column("accounts", sa.Column("imap_password", sa.String(512), nullable=True))
    op.add_column("accounts", sa.Column("smtp_password", sa.String(512), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("state", sa.String(20), nullable=False, server_default="created"),
    )
    op.add_column(
        "accounts",
        sa.Column("sync_lookback_days", sa.Integer(), nullable=False, server_default="180"),
    )
    op.add_column(
        "accounts",
        sa.Column("embedding_lookback_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "accounts",
        sa.Column("spam_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    """Remove v2 schema changes."""
    op.drop_column("accounts", "spam_enabled")
    op.drop_column("accounts", "embedding_lookback_days")
    op.drop_column("accounts", "sync_lookback_days")
    op.drop_column("accounts", "state")
    op.drop_column("accounts", "smtp_password")
    op.drop_column("accounts", "imap_password")
    op.drop_index("idx_job_state_name", table_name="job_states")
    op.drop_table("job_states")
    op.drop_table("settings")
