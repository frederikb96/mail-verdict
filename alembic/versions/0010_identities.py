"""Add identities -- addresses a mail account may send as.

No foreign key onto accounts (PostIMAP-owned), consistent with every
other MailVerdict-owned table -- see models.py's module docstring.

Revision ID: 0010_identities
Revises: 0009_queue_state_drop_batch_size
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0010_identities"
down_revision: str | None = "0009_queue_state_drop_batch_size"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the identities table and its two functional unique indexes."""
    op.create_table(
        "identities",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_identities_account_id", "identities", ["account_id"])
    # Neither uniqueness rule is a plain column list -- one is
    # case-insensitive, the other is partial -- so both are raw SQL rather
    # than op.create_index(), the same way the coalesce-expression indexes
    # in 0002 and 0008 are.
    op.execute(
        "CREATE UNIQUE INDEX uq_identities_account_email "
        "ON identities (account_id, lower(email))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_identities_default "
        "ON identities (account_id) WHERE is_default"
    )


def downgrade() -> None:
    """Drop the identities table and its indexes."""
    op.execute("DROP INDEX uq_identities_default")
    op.execute("DROP INDEX uq_identities_account_email")
    op.drop_index("idx_identities_account_id", table_name="identities")
    op.drop_table("identities")
