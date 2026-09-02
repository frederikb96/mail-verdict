"""Heal calendar_intake rows the pre-0014 bug already produced.

Before 0014 introduced the 'pending' status, _write_intake_row() wrote
the terminal status (e.g. 'imported') *before* _apply() created the
object it describes. Anything interrupting between the two left a row
saying 'imported' with object_id NULL -- nothing was actually imported,
the never-classify-twice gate then keeps that message unprocessable
forever (ON CONFLICT DO NOTHING on (account_id, msg_key)), and the UI
reads the row as a completed import into a named calendar that holds no
such object.

0014 stops new rows from reaching that shape; it does nothing for rows
the old code already produced. A successful import always sets
object_id, so 'imported' with a NULL object_id can only be this strand
-- rewritten to 'pending' so process_arrival()'s own retry-on-pending
logic picks it back up the next time this message's UID is seen (a
resync, or a future redelivery), rather than the message staying
unprocessable indefinitely.

Revision ID: 0017_heal_stranded_intake
Revises: 0016_intake_pending_review
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0017_heal_stranded_intake"
down_revision: str | None = "0016_intake_pending_review"
branch_labels: str | None = None
depends_on: str | None = None

calendar_intake = sa.table(
    "calendar_intake",
    sa.column("status", sa.Text),
    sa.column("object_id", sa.Uuid),
)


def upgrade() -> None:
    """Rewrite every stranded 'imported'-with-no-object row to 'pending'."""
    op.execute(
        calendar_intake.update()
        .where(calendar_intake.c.status == "imported", calendar_intake.c.object_id.is_(None))
        .values(status="pending")
    )


def downgrade() -> None:
    """Not reversible -- there is no way to tell a row this heals apart
    from one that was already genuinely 'pending' for an unrelated
    reason (row 114's own crash-safety window), so downgrading does
    nothing rather than guessing wrong in either direction."""
