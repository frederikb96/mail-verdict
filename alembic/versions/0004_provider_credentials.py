"""Add provider_credentials -- encrypted AI provider API keys.

Revision ID: 0002_provider_credentials
Revises: 0001_v1_baseline
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_provider_credentials"
down_revision = "0003_queue_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the provider_credentials table."""
    op.create_table(
        "provider_credentials",
        sa.Column("provider", sa.String(50), primary_key=True),
        sa.Column("encrypted_key", sa.LargeBinary, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Drop the provider_credentials table."""
    op.drop_table("provider_credentials")
