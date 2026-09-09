"""Add alerts.folder_id -- what "which folders alert" actually scopes.

alerts carried account_id/message_id but no folder, so a mail alert for
Sent, Drafts, Junk, Trash or Archive was indistinguishable from an inbox
one once inserted, and the durable bell list could not honour the same
per-device folder preference the SSE and push paths already compute.
Nullable and unbacked by a foreign key, the same shape as account_id and
message_id on this table -- a "reminder" kind alert has no folder at all,
and an existing row predating this column stays NULL rather than being
backfilled from a message lookup that may no longer resolve.

Revision ID: 0024_alerts_folder_id
Revises: 0023_vapid_keypair
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0024_alerts_folder_id"
down_revision: str | None = "0023_vapid_keypair"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add alerts.folder_id."""
    op.add_column("alerts", sa.Column("folder_id", sa.Uuid, nullable=True))


def downgrade() -> None:
    """Drop alerts.folder_id."""
    op.drop_column("alerts", "folder_id")
