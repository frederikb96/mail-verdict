"""Orders and tickets: the orders/order_mails/order_identifiers/order_jobs
tables, account_prefs.orders_enabled, and inserting the `orders` pipeline
stage into the current pipeline revision (or into build_migrated_definition's
output, for a fresh install -- see pipeline/revisions.py's
insert_orders_stage, used by both so a fresh install and an upgraded one
end up alike).

No foreign key from order_mails/order_identifiers/order_jobs onto any
PostIMAP-owned table, and none from order_mails/order_jobs onto orders
either for order_jobs (see database/models.py's OrderJob docstring) --
consistent with every other MailVerdict-owned table.

Revision ID: 0038_orders
Revises: 0037_glacier_sweep_refusal
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038_orders"
down_revision: str | None = "0037_glacier_sweep_refusal"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the orders tables, add account_prefs.orders_enabled, and
    insert the orders stage into the current pipeline revision."""
    op.add_column(
        "account_prefs",
        sa.Column("orders_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("merchant", sa.Text(), nullable=False, server_default=""),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("icon", sa.Text(), nullable=False, server_default="receipt"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_stale", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("mail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_mail_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_mail_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_orders_list", "orders", [sa.text("last_mail_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("written_at IS NOT NULL"),
    )

    op.create_table(
        "order_mails",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "order_id", sa.Uuid(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("msg_key", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("from_addr", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attached_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "attached_by IN ('ai', 'thread', 'user')", name="ck_order_mails_attached_by",
        ),
        sa.UniqueConstraint("account_id", "msg_key", name="uq_order_mails_account_msg_key"),
    )
    op.create_index("idx_order_mails_order_received", "order_mails", ["order_id", "received_at"])
    op.create_index("idx_order_mails_account_thread", "order_mails", ["account_id", "thread_id"])

    op.create_table(
        "order_identifiers",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "order_id", sa.Uuid(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("value_norm", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('order_number', 'booking_code', 'tracking_number', "
            "'invoice_number', 'ticket_number')",
            name="ck_order_identifiers_kind",
        ),
        sa.UniqueConstraint(
            "order_id", "value_norm", name="uq_order_identifiers_order_value_norm",
        ),
    )
    op.create_index("idx_order_identifiers_value_norm", "order_identifiers", ["value_norm"])

    op.create_table(
        "order_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("msg_key", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("filter_reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("decision", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint("kind IN ('mail', 'write')", name="ck_order_jobs_kind"),
        sa.CheckConstraint(
            "origin IN ('live', 'thread', 'catchup', 'manual')", name="ck_order_jobs_origin",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'done', 'skipped', 'failed')",
            name="ck_order_jobs_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('attached', 'created', 'none', 'skipped', 'detached', 'written')",
            name="ck_order_jobs_outcome",
        ),
        sa.CheckConstraint(
            "kind <> 'mail' OR (account_id IS NOT NULL AND msg_key IS NOT NULL)",
            name="ck_order_jobs_mail_fields",
        ),
        sa.CheckConstraint(
            "kind <> 'write' OR order_id IS NOT NULL", name="ck_order_jobs_write_fields",
        ),
    )
    op.create_index(
        "uq_order_jobs_mail", "order_jobs", ["account_id", "msg_key"], unique=True,
        postgresql_where=sa.text("kind = 'mail'"),
    )
    op.create_index(
        "uq_order_jobs_write", "order_jobs", ["order_id"], unique=True,
        postgresql_where=sa.text("kind = 'write' AND status = 'pending'"),
    )
    op.create_index(
        "ix_order_jobs_claim", "order_jobs", ["priority", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    _insert_orders_stage_into_current_revision()


def _insert_orders_stage_into_current_revision() -> None:
    """Read the current pipeline revision (if any), and append a new one
    with the orders stage inserted -- run in Python so the exact same
    function (insert_orders_stage) is used here and is unit-tested on its
    own. A no-op when there is no revision yet (a database that has never
    run alembic/versions/0006_pipeline.py's own data migration) or when
    the current revision already carries an orders stage."""
    from mail_verdict.pipeline.revisions import insert_orders_stage

    bind = op.get_bind()

    revisions = sa.table(
        "pipeline_revisions",
        sa.column("revision", sa.Integer),
        sa.column("document", postgresql.JSONB),
        sa.column("note", sa.Text),
    )
    row = bind.execute(
        sa.select(revisions.c.revision, revisions.c.document)
        .order_by(revisions.c.revision.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return

    document: dict[str, Any] = row.document
    stages = document.get("stages", [])
    new_stages = insert_orders_stage(stages)
    if new_stages == stages:
        return

    new_document = {**document, "stages": new_stages}
    bind.execute(
        sa.insert(revisions).values(
            document=json.dumps(new_document), note="Add the orders stage",
        )
    )


def downgrade() -> None:
    """Drop the orders tables and account_prefs.orders_enabled. The
    pipeline revision this migration may have appended is left in place,
    consistent with pipeline_revisions being append-only audit history
    (see 0006_pipeline.py's downgrade, which leaves its own data
    migration's revision in place for the same reason)."""
    op.drop_table("order_jobs")
    op.drop_index("idx_order_identifiers_value_norm", table_name="order_identifiers")
    op.drop_table("order_identifiers")
    op.drop_index("idx_order_mails_account_thread", table_name="order_mails")
    op.drop_index("idx_order_mails_order_received", table_name="order_mails")
    op.drop_table("order_mails")
    op.drop_index("ix_orders_list", table_name="orders")
    op.drop_table("orders")
    op.drop_column("account_prefs", "orders_enabled")
