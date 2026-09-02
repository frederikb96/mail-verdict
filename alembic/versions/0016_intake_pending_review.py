"""Add 'pending_review' to calendar_intake.status.

An emailed REQUEST or CANCEL naming a UID already held never auto-applies
now, whoever it claims to be from -- an authenticated ORGANIZER match
raised the price of forging one but did not close it, since the same
`.ics` that hands an attacker the UID hands them the ORGANIZER address
too (both are lines in the invitation every attendee receives). Every
such message becomes a row a person confirms by hand instead, recorded
with this status rather than 'updated'/'cancelled'/'unauthorized'.

'unauthorized' stays in the CHECK constraint -- nothing produces it going
forward, but narrowing a CHECK a populated table is already subject to
is exactly the trap this project's own migrations have hit before.

Revision ID: 0016_intake_pending_review
Revises: 0015_calendar_replies_set_null
"""

from __future__ import annotations

from alembic import op

revision: str = "0016_intake_pending_review"
down_revision: str | None = "0015_calendar_replies_set_null"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen ck_calendar_intake_status to allow 'pending_review'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored', 'unauthorized', 'pending', 'pending_review')",
    )


def downgrade() -> None:
    """Narrow ck_calendar_intake_status back, without 'pending_review'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored', 'unauthorized', 'pending')",
    )
