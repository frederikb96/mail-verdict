"""calendar_replies.identity_id: ON DELETE CASCADE -> ON DELETE SET NULL.

The table exists to keep RSVP history, but CASCADE deleted every reply an
identity ever sent the moment that identity was removed -- the same
un-linking calendar_prefs.identity_id already does on identity deletion,
not a destruction of the history the table exists to keep.

Revision ID: 0015_calendar_replies_set_null
Revises: 0014_intake_pending
"""

from __future__ import annotations

from alembic import op

revision: str = "0015_calendar_replies_set_null"
down_revision: str | None = "0014_intake_pending"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "calendar_replies_identity_id_fkey"


def upgrade() -> None:
    """Make identity_id nullable and switch its FK to SET NULL."""
    op.alter_column("calendar_replies", "identity_id", nullable=True)
    op.drop_constraint(_CONSTRAINT, "calendar_replies", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT, "calendar_replies", "identities",
        ["identity_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    """Restore CASCADE and NOT NULL -- rows left NULL by a prior SET NULL
    would violate the restored constraint, so this only round-trips
    cleanly on a database with no such rows."""
    op.drop_constraint(_CONSTRAINT, "calendar_replies", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT, "calendar_replies", "identities",
        ["identity_id"], ["id"], ondelete="CASCADE",
    )
    op.alter_column("calendar_replies", "identity_id", nullable=False)
