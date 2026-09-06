"""Let calendar_prefs.is_enabled say that nobody has decided.

A calendar with no prefs row at all falls back to whether the collection
can hold an event, so a to-do-only one stays out of the sidebar. A row
written for some unrelated reason -- a colour override, a per-view
checkbox -- answered the question anyway, because the column was NOT NULL
and defaulted to true: the fallback was reached only by collections that
had never been touched at all.

NULL is the third state that fixes it, and the existing rows need it too.
Before this revision a stored `true` could not be distinguished from a
default nobody chose, so every one of them becomes NULL and resolves the
same way a missing row does -- no change for a calendar that holds events,
and a to-do list stops being pinned on. A stored `false` was always a
deliberate choice and is left alone, as is anything switched on from here.

Revision ID: 0021_calendar_prefs_undecided
Revises: 0020_pending_sends
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0021_calendar_prefs_undecided"
down_revision: str | None = "0020_pending_sends"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Make is_enabled nullable, drop its default, and clear every true."""
    op.alter_column(
        "calendar_prefs", "is_enabled",
        existing_type=sa.Boolean(), nullable=True, server_default=None,
    )
    op.execute("UPDATE calendar_prefs SET is_enabled = NULL WHERE is_enabled")


def downgrade() -> None:
    """An undecided row goes back to enabled -- the value the column
    carried for it before, and the only one it can carry again."""
    op.execute("UPDATE calendar_prefs SET is_enabled = true WHERE is_enabled IS NULL")
    op.alter_column(
        "calendar_prefs", "is_enabled",
        existing_type=sa.Boolean(), nullable=False, server_default="true",
    )
