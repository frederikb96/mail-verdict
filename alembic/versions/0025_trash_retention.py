"""Add account_prefs.trash_retention_days -- the periodic sweep's own
per-account switch.

NULL (the default) is off, the same shape as calendar.default_calendar_id
in the settings table: nothing here until a person sets it, and the
sweep (retention/sweep.py) skips any account where it is still NULL.

Revision ID: 0025_trash_retention
Revises: 0024_alerts_folder_id
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0025_trash_retention"
down_revision: str | None = "0024_alerts_folder_id"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add account_prefs.trash_retention_days."""
    op.add_column(
        "account_prefs", sa.Column("trash_retention_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop account_prefs.trash_retention_days."""
    op.drop_column("account_prefs", "trash_retention_days")
