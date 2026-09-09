"""Add vapid_keypair -- the server's Web Push signing identity.

A single row (id fixed at 1, enforced by the primary key itself), generated
on first use rather than provisioned: nothing seeds this table, the
repository that owns it inserts the row the first time a public key or a
push send is asked for. The private key is stored encrypted with the same
AES-256-GCM key and format core/encryption.py already uses for provider API
keys (settings/credentials.py) -- one encryption mechanism, one key,
whether what it protects is a model provider's secret or this one.

Revision ID: 0023_vapid_keypair
Revises: 0022_alerts_and_push
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0023_vapid_keypair"
down_revision: str | None = "0022_alerts_and_push"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create vapid_keypair."""
    op.create_table(
        "vapid_keypair",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("encrypted_private_key", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="ck_vapid_keypair_singleton"),
    )


def downgrade() -> None:
    """Drop vapid_keypair."""
    op.drop_table("vapid_keypair")
