"""Add calendar_prefs, calendar_intake and calendar_replies.

No foreign key onto dav_collections/dav_accounts/dav_objects/outbox
(PostIMAP-owned), consistent with every other MailVerdict-owned table --
see models.py's module docstring. identity_id does carry a real foreign
key onto identities: both tables are MailVerdict-owned and migrated
together, so there is no grant boundary to cross, and ON DELETE SET NULL
means deleting an identity un-links its calendars rather than leaving a
dangling reference.

Revision ID: 0011_calendar
Revises: 0010_identities
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0011_calendar"
down_revision: str | None = "0010_identities"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create calendar_prefs, calendar_intake and calendar_replies."""
    op.create_table(
        "calendar_prefs",
        sa.Column("collection_id", sa.Uuid, primary_key=True),
        sa.Column(
            "identity_id", sa.Uuid,
            sa.ForeignKey("identities.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("intake", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_visible", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("color_override", sa.Text, nullable=True),
    )
    op.create_index("idx_calendar_prefs_identity", "calendar_prefs", ["identity_id"])
    # At most one intake calendar per identity -- a WHERE-partial index,
    # not a plain UniqueConstraint, the same reason uq_identities_default
    # in 0010 is raw SQL.
    op.execute(
        "CREATE UNIQUE INDEX uq_calendar_prefs_intake "
        "ON calendar_prefs (identity_id) WHERE intake"
    )

    op.create_table(
        "calendar_intake",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("msg_key", sa.Text, nullable=False),
        sa.Column("ical_uid", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("sequence", sa.Integer, nullable=True),
        sa.Column("recurrence_id", sa.Text, nullable=True),
        sa.Column("dav_account_id", sa.Uuid, nullable=True),
        sa.Column("collection_id", sa.Uuid, nullable=True),
        sa.Column("object_id", sa.Uuid, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "method IN ('REQUEST', 'REPLY', 'CANCEL', 'COUNTER')",
            name="ck_calendar_intake_method",
        ),
        sa.CheckConstraint(
            "status IN ('imported', 'updated', 'cancelled', 'ignored_stale', "
            "'unlinked', 'failed', 'ignored')",
            name="ck_calendar_intake_status",
        ),
    )
    # The never-classify-twice gate's calendar counterpart: one row per
    # durable message identity, ever -- see database/msg_key.py and
    # docs/architecture.md's "Never classifying the same message twice".
    op.create_unique_constraint(
        "uq_calendar_intake_account_msg_key", "calendar_intake", ["account_id", "msg_key"],
    )
    op.create_index("idx_calendar_intake_object", "calendar_intake", ["object_id"])

    op.create_table(
        "calendar_replies",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("object_id", sa.Uuid, nullable=False),
        sa.Column("recurrence_id", sa.Text, nullable=True),
        sa.Column(
            "identity_id", sa.Uuid,
            sa.ForeignKey("identities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("partstat", sa.Text, nullable=False),
        sa.Column("outbox_id", sa.Uuid, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "partstat IN ('accepted', 'declined', 'tentative')",
            name="ck_calendar_replies_partstat",
        ),
    )
    # Insert-only: respond() adds a row per attempt rather than updating one
    # in place, so "the last outbox row this identity's respond produced"
    # (own_reply) is simply the latest row for (object_id, recurrence_id,
    # identity_id) -- this index is what makes that lookup cheap.
    op.create_index(
        "idx_calendar_replies_object",
        "calendar_replies", ["object_id", "recurrence_id", "identity_id", "created_at"],
    )


def downgrade() -> None:
    """Drop calendar_prefs, calendar_intake and calendar_replies."""
    op.drop_table("calendar_replies")
    op.drop_table("calendar_intake")
    op.execute("DROP INDEX uq_calendar_prefs_intake")
    op.drop_index("idx_calendar_prefs_identity", table_name="calendar_prefs")
    op.drop_table("calendar_prefs")
