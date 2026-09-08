"""Add alerts and push_subscriptions, and two nullable reminder columns on
calendar_prefs -- the schema three later features share: a push-delivered
alert for new mail, a calendar reminder delivered the same way, and the
per-calendar reminder defaults an event editor pre-fills from.

alerts is both the delivery queue and the durable record: nothing but
dedupe_key distinguishes a "please fire" row from a "this already fired"
row, since one row is both before and after delivered_at is stamped. Its
unique index is the entire fires-exactly-once mechanism, the same
ON CONFLICT DO NOTHING discipline Verdict and CalendarIntake already use
for their own durability gates.

push_subscriptions is MailVerdict-owned with no foreign key onto anything
of PostIMAP's, consistent with every other table here. Per-device alert
preferences live on the subscription row rather than in browser storage,
since a subscription is the only genuinely per-device thing a system with
no login has.

default_reminder_minutes and reminders_enabled are added nullable with no
default of any kind, so a row written for an unrelated reason (a colour
override) never answers either question by accident -- the same NULL-
means-nobody-decided shape calendar_prefs.is_enabled had to be migrated
into after the fact (0021), gotten right from the start here.

Revision ID: 0022_alerts_and_push
Revises: 0021_calendar_prefs_undecided
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0022_alerts_and_push"
down_revision: str | None = "0021_calendar_prefs_undecided"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create alerts and push_subscriptions; add the two calendar_prefs columns."""
    op.create_table(
        "alerts",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("deliver_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("account_id", sa.Uuid, nullable=True),
        sa.Column("message_id", sa.Uuid, nullable=True),
        sa.Column("object_id", sa.Uuid, nullable=True),
        sa.Column("recurrence_id", sa.Text, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("kind IN ('mail', 'reminder')", name="ck_alerts_kind"),
    )
    op.create_unique_constraint("uq_alerts_dedupe_key", "alerts", ["dedupe_key"])
    # Partial: a delivered row never needs to be found by deliver_at again,
    # so the dispatcher's due-claim query (SELECT ... FOR UPDATE SKIP
    # LOCKED) is the only thing this index has to serve -- the same shape
    # as PendingSend's idx_pending_sends_due.
    op.execute(
        "CREATE INDEX idx_alerts_due ON alerts (deliver_at) WHERE delivered_at IS NULL"
    )

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("p256dh", sa.Text, nullable=False),
        sa.Column("auth", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=True),
        sa.Column("alert_folder_ids", sa.ARRAY(sa.Uuid), nullable=True),
        sa.Column("reminders_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_push_subscriptions_endpoint", "push_subscriptions", ["endpoint"],
    )

    op.add_column(
        "calendar_prefs",
        sa.Column("default_reminder_minutes", sa.Integer, nullable=True),
    )
    op.add_column(
        "calendar_prefs",
        sa.Column("reminders_enabled", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    """Drop the two calendar_prefs columns, push_subscriptions and alerts."""
    op.drop_column("calendar_prefs", "reminders_enabled")
    op.drop_column("calendar_prefs", "default_reminder_minutes")
    op.drop_table("push_subscriptions")
    op.execute("DROP INDEX idx_alerts_due")
    op.drop_table("alerts")
