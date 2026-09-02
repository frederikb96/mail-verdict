"""Add 'unauthorized' to calendar_intake.status -- an incoming
REQUEST/CANCEL/REPLY whose sender does not match the stored object's
organizer (or, for REPLY, the attendee it claims to speak for) is left
untouched rather than applied, and recorded distinctly from 'ignored_stale'
so a person reviewing the invitation can tell the two apart.

Revision ID: 0013_intake_unauthorized
Revises: 0012_calendar_links_revision
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_intake_unauthorized"
down_revision: str | None = "0012_calendar_links_revision"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen ck_calendar_intake_status to allow 'unauthorized'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored', 'unauthorized')",
    )


def downgrade() -> None:
    """Narrow ck_calendar_intake_status back, without 'unauthorized'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored')",
    )
