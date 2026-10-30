"""Orders wallet: kinds, event dates, activity timestamps, typed facts,
documents and files.

orders gains kind/kind_set_by (seeded once from the icon, so the lists
work before any rewrite), event_start/event_end, file_count and the
first/last activity pair (equal to the mail pair until a file exists);
the list index moves to last_activity_at. order_identifiers becomes
order_facts in place: every identifier keeps its id, kind, value and
normalised value and becomes an identifier-kind fact. order_documents and
order_files are new, and order_jobs learns the 'file' job kind.

Revision ID: 0047_orders_wallet
Revises: 0046_drop_unused_received_index
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047_orders_wallet"
down_revision: str | None = "0046_drop_unused_received_index"
branch_labels: str | None = None
depends_on: str | None = None

_FACT_KINDS = (
    "'order_number', 'booking_code', 'ticket_number', 'invoice_number', 'tracking_number', "
    "'date', 'time', 'amount', 'place', 'seat', 'person', 'code', 'link', 'other'"
)
_OLD_IDENTIFIER_KINDS = (
    "'order_number', 'booking_code', 'tracking_number', 'invoice_number', 'ticket_number'"
)


def upgrade() -> None:
    _upgrade_orders()
    _create_files_and_documents()
    _upgrade_identifiers_to_facts()
    _upgrade_jobs()


def _upgrade_orders() -> None:
    op.add_column(
        "orders", sa.Column("kind", sa.Text(), nullable=False, server_default="order"),
    )
    op.add_column(
        "orders", sa.Column("kind_set_by", sa.Text(), nullable=False, server_default="ai"),
    )
    op.add_column("orders", sa.Column("event_start", sa.Date(), nullable=True))
    op.add_column("orders", sa.Column("event_end", sa.Date(), nullable=True))
    op.add_column(
        "orders", sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders", sa.Column("first_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE orders SET first_activity_at = first_mail_at, last_activity_at = last_mail_at, "
        "kind = CASE "
        "WHEN icon IN ('ticket', 'train', 'plane', 'bus', 'car', 'bed') THEN 'ticket' "
        "WHEN icon = 'receipt' THEN 'other' "
        "ELSE 'order' END"
    )
    op.create_check_constraint("ck_orders_kind", "orders", "kind IN ('ticket', 'order', 'other')")
    op.create_check_constraint("ck_orders_kind_set_by", "orders", "kind_set_by IN ('ai', 'user')")

    op.drop_index("ix_orders_list", table_name="orders")
    op.create_index(
        "ix_orders_list", "orders",
        [sa.text("last_activity_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("written_at IS NOT NULL"),
    )


def _create_files_and_documents() -> None:
    op.create_table(
        "order_files",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id", ondelete="CASCADE")),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'attached', 'failed')", name="ck_order_files_status",
        ),
    )
    op.create_index("idx_order_files_order", "order_files", ["order_id"])

    op.create_table(
        "order_documents",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "order_id", sa.Uuid(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("msg_key", sa.Text(), nullable=True),
        sa.Column("attachment_id", sa.Uuid(), nullable=True),
        sa.Column("file_id", sa.Uuid(), sa.ForeignKey("order_files.id", ondelete="CASCADE")),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_chars_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "barcodes", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("extractor_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "extracted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("source IN ('mail', 'file')", name="ck_order_documents_source"),
        sa.CheckConstraint(
            "method IN ('text_layer', 'ocr', 'pkpass', 'ics', 'xml', 'html', 'image', "
            "'none', 'failed')",
            name="ck_order_documents_method",
        ),
    )
    op.create_index("idx_order_documents_order", "order_documents", ["order_id"])
    op.create_index(
        "uq_order_documents_mail", "order_documents", ["account_id", "msg_key", "attachment_id"],
        unique=True, postgresql_where=sa.text("source = 'mail'"),
    )
    op.create_index(
        "uq_order_documents_file", "order_documents", ["file_id"],
        unique=True, postgresql_where=sa.text("source = 'file'"),
    )


def _upgrade_identifiers_to_facts() -> None:
    op.rename_table("order_identifiers", "order_facts")
    op.execute("ALTER INDEX order_identifiers_pkey RENAME TO order_facts_pkey")
    op.execute(
        "ALTER TABLE order_facts "
        "RENAME CONSTRAINT order_identifiers_order_id_fkey TO order_facts_order_id_fkey"
    )
    op.execute("ALTER INDEX idx_order_identifiers_value_norm RENAME TO idx_order_facts_value_norm")
    op.drop_constraint("ck_order_identifiers_kind", "order_facts", type_="check")
    op.drop_constraint("uq_order_identifiers_order_value_norm", "order_facts", type_="unique")

    op.alter_column("order_facts", "value_norm", existing_type=sa.Text(), nullable=True)
    op.add_column("order_facts", sa.Column("label", sa.Text(), nullable=True))
    op.add_column("order_facts", sa.Column("carrier", sa.Text(), nullable=True))
    op.add_column("order_facts", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.add_column("order_facts", sa.Column("barcode_index", sa.Integer(), nullable=True))
    op.add_column("order_facts", sa.Column("date_value", sa.Date(), nullable=True))
    op.add_column(
        "order_facts", sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "order_facts", sa.Column("source", sa.Text(), nullable=False, server_default="ai"),
    )
    op.add_column(
        "order_facts",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        """
        UPDATE order_facts f
        SET label = CASE f.kind
                WHEN 'order_number' THEN 'Order number'
                WHEN 'booking_code' THEN 'Booking code'
                WHEN 'ticket_number' THEN 'Ticket number'
                WHEN 'invoice_number' THEN 'Invoice number'
                WHEN 'tracking_number' THEN 'Tracking number'
            END,
            position = ranked.position
        FROM (
            SELECT id, row_number() OVER (PARTITION BY order_id ORDER BY kind, value) - 1
                AS position
            FROM order_facts
        ) ranked
        WHERE ranked.id = f.id
        """
    )
    op.alter_column("order_facts", "label", existing_type=sa.Text(), nullable=False)

    op.create_check_constraint("ck_order_facts_kind", "order_facts", f"kind IN ({_FACT_KINDS})")
    op.create_check_constraint(
        "ck_order_facts_source", "order_facts", "source IN ('ai', 'code', 'user')",
    )
    op.create_foreign_key(
        "order_facts_document_id_fkey", "order_facts", "order_documents",
        ["document_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "uq_order_facts_order_value_norm", "order_facts", ["order_id", "value_norm"],
        unique=True, postgresql_where=sa.text("value_norm IS NOT NULL"),
    )
    op.create_index(
        "uq_order_facts_order_code", "order_facts", ["order_id", "document_id", "barcode_index"],
        unique=True, postgresql_where=sa.text("document_id IS NOT NULL"),
    )
    op.create_index("idx_order_facts_order_position", "order_facts", ["order_id", "position"])


def _upgrade_jobs() -> None:
    op.add_column("order_jobs", sa.Column("file_id", sa.Uuid(), nullable=True))
    op.drop_constraint("ck_order_jobs_kind", "order_jobs", type_="check")
    op.create_check_constraint(
        "ck_order_jobs_kind", "order_jobs", "kind IN ('mail', 'write', 'file')",
    )
    op.drop_constraint("ck_order_jobs_origin", "order_jobs", type_="check")
    op.create_check_constraint(
        "ck_order_jobs_origin", "order_jobs",
        "origin IN ('live', 'thread', 'catchup', 'manual', 'upload')",
    )
    op.create_check_constraint(
        "ck_order_jobs_file_fields", "order_jobs", "kind <> 'file' OR file_id IS NOT NULL",
    )
    op.create_index(
        "uq_order_jobs_file", "order_jobs", ["file_id"],
        unique=True, postgresql_where=sa.text("kind = 'file'"),
    )


def downgrade() -> None:
    op.drop_index("uq_order_jobs_file", table_name="order_jobs")
    op.drop_constraint("ck_order_jobs_file_fields", "order_jobs", type_="check")
    op.execute("DELETE FROM order_jobs WHERE kind = 'file' OR origin = 'upload'")
    op.drop_constraint("ck_order_jobs_origin", "order_jobs", type_="check")
    op.create_check_constraint(
        "ck_order_jobs_origin", "order_jobs",
        "origin IN ('live', 'thread', 'catchup', 'manual')",
    )
    op.drop_constraint("ck_order_jobs_kind", "order_jobs", type_="check")
    op.create_check_constraint("ck_order_jobs_kind", "order_jobs", "kind IN ('mail', 'write')")
    op.drop_column("order_jobs", "file_id")

    op.drop_index("idx_order_facts_order_position", table_name="order_facts")
    op.drop_index("uq_order_facts_order_code", table_name="order_facts")
    op.drop_index("uq_order_facts_order_value_norm", table_name="order_facts")
    op.drop_constraint("order_facts_document_id_fkey", "order_facts", type_="foreignkey")
    op.execute(f"DELETE FROM order_facts WHERE kind NOT IN ({_OLD_IDENTIFIER_KINDS})")
    op.drop_constraint("ck_order_facts_source", "order_facts", type_="check")
    op.drop_constraint("ck_order_facts_kind", "order_facts", type_="check")
    for column in (
        "created_at", "source", "position", "date_value", "barcode_index", "document_id",
        "carrier", "label",
    ):
        op.drop_column("order_facts", column)
    op.alter_column("order_facts", "value_norm", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint(
        "uq_order_identifiers_order_value_norm", "order_facts", ["order_id", "value_norm"],
    )
    op.create_check_constraint(
        "ck_order_identifiers_kind", "order_facts", f"kind IN ({_OLD_IDENTIFIER_KINDS})",
    )
    op.execute("ALTER INDEX idx_order_facts_value_norm RENAME TO idx_order_identifiers_value_norm")
    op.execute(
        "ALTER TABLE order_facts "
        "RENAME CONSTRAINT order_facts_order_id_fkey TO order_identifiers_order_id_fkey"
    )
    op.execute("ALTER INDEX order_facts_pkey RENAME TO order_identifiers_pkey")
    op.rename_table("order_facts", "order_identifiers")

    op.drop_table("order_documents")
    op.drop_table("order_files")

    op.drop_index("ix_orders_list", table_name="orders")
    op.create_index(
        "ix_orders_list", "orders", [sa.text("last_mail_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("written_at IS NOT NULL"),
    )
    op.drop_constraint("ck_orders_kind_set_by", "orders", type_="check")
    op.drop_constraint("ck_orders_kind", "orders", type_="check")
    for column in (
        "last_activity_at", "first_activity_at", "file_count", "event_end", "event_start",
        "kind_set_by", "kind",
    ):
        op.drop_column("orders", column)
