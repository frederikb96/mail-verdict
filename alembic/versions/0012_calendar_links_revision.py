"""Add calendar_links_revision -- the optimistic-concurrency counter for
PUT /api/calendar/links, the same base_revision idea /api/pipeline uses.

A single counter rather than one per identity: the whole mapping document
is replaced in one write (PUT, not PATCH), so one shared revision is
enough to catch two editors racing on the document as a whole.

Revision ID: 0012_calendar_links_revision
Revises: 0011_calendar
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0012_calendar_links_revision"
down_revision: str | None = "0011_calendar"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the singleton calendar_links_revision row."""
    op.create_table(
        "calendar_links_revision",
        sa.Column("singleton", sa.Boolean, primary_key=True, server_default="true"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("singleton", name="ck_calendar_links_revision_singleton"),
    )
    op.execute("INSERT INTO calendar_links_revision (revision) VALUES (0)")


def downgrade() -> None:
    """Drop calendar_links_revision."""
    op.drop_table("calendar_links_revision")
