"""Add 'pending' to calendar_intake.status -- the gate row is written with
this status before the object write it describes is attempted, and
promoted to the real terminal status once that write lands. Previously
the terminal status was written first, so anything interrupting between
the two left a row saying e.g. 'imported' with a NULL object_id --
`ON CONFLICT DO NOTHING` on (account_id, msg_key) then made that message
unprocessable forever, while the UI read the row as if the import had
actually happened.

Revision ID: 0014_intake_pending
Revises: 0013_intake_unauthorized
"""

from __future__ import annotations

from alembic import op

revision: str = "0014_intake_pending"
down_revision: str | None = "0013_intake_unauthorized"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Widen ck_calendar_intake_status to allow 'pending'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored', 'unauthorized', 'pending')",
    )


def downgrade() -> None:
    """Narrow ck_calendar_intake_status back, without 'pending'."""
    op.drop_constraint("ck_calendar_intake_status", "calendar_intake", type_="check")
    op.create_check_constraint(
        "ck_calendar_intake_status",
        "calendar_intake",
        "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
        "'unlinked', 'failed', 'ignored', 'unauthorized')",
    )
