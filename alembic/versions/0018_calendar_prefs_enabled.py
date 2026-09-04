"""Split calendar visibility into two independently-settable levels.

calendar_prefs.is_visible already gates the per-view checkbox: whether a
calendar's events are drawn right now. There was no way to also remove a
calendar from that list entirely (declutter it out of the sidebar and the
event editor's Calendar picker) without losing the per-view toggle's own
state -- the two ideas shared one column, so setting either one meant
setting both.

is_enabled is the second level: whether the calendar is offered at all.
An event API filtering on is_visible alone would let a disabled
calendar's events keep rendering even though it no longer appears
anywhere a person could turn it back off, so the two are combined at the
one place that decides what a month view returns.

Revision ID: 0018_calendar_prefs_enabled
Revises: 0017_heal_stranded_intake
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0018_calendar_prefs_enabled"
down_revision: str | None = "0017_heal_stranded_intake"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add is_enabled, defaulting every existing calendar to enabled."""
    op.add_column(
        "calendar_prefs",
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("calendar_prefs", "is_enabled")
