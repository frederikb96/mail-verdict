"""Add the glacier: a per-account place a message can be moved to where it
leaves the mail server for good and lives on only in this database.

account_prefs gains the per-account switch (glacier_enabled), the
synthetic folder id used everywhere a real folder_id is used
(glacier_folder_id), and the automatic-sweep age in days
(glacier_auto_days, NULL meaning the sweep is off).

glacier_messages is column-compatible with messages: every column of
that table exists here with the same name and type
(tests/unit/test_glacier_columns.py asserts this, derived from the
model), which is what makes a read that must span both tables a
mechanical union rather than a maintained parallel query. search_vector
replicates PostIMAP's own definition verbatim so ranking behaves
identically for a glaciered message; subject and from_addr each get the
same partial trigram index PostIMAP's own messages table carries, for
the same typo-tolerant fallback search.

No foreign key onto anything PostIMAP owns, and no database VIEW
unioning the two tables -- a view would create a dependency object on a
PostIMAP-owned table that a later migration of PostIMAP's own could not
then alter without erroring "other objects depend on it", from another
repository, on someone else's deploy. The union is built in SQLAlchemy,
per query, instead.

Revision ID: 0034_glacier
Revises: 0033_message_action_submissions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034_glacier"
down_revision: str | None = "0033_message_action_submissions"
branch_labels: str | None = None
depends_on: str | None = None

# PostIMAP's own search_vector definition (see its consumer contract's
# "Full-text search" section), reproduced verbatim so a glaciered
# message ranks identically to a live one. left()-bounded for the same
# reason PostIMAP bounds it: a tsvector's internal representation is
# capped just under 1MB regardless of how it was built, and an unbounded
# body_text can abort the insert outright.
_SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('simple', coalesce(left(subject, 2000), '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(left(from_addr, 500), '')), 'B') || "
    "setweight(to_tsvector('simple', coalesce(left(to_addrs::text, 1000), '')), 'C') || "
    "setweight(to_tsvector('simple', coalesce(left(body_text, 200000), '')), 'D')"
)


def upgrade() -> None:
    """Add the glacier's account_prefs columns and its two tables."""
    op.add_column(
        "account_prefs",
        sa.Column("glacier_enabled", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "account_prefs", sa.Column("glacier_folder_id", sa.Uuid, nullable=True),
    )
    op.create_unique_constraint(
        "uq_account_prefs_glacier_folder_id", "account_prefs", ["glacier_folder_id"],
    )
    op.add_column(
        "account_prefs", sa.Column("glacier_auto_days", sa.Integer, nullable=True),
    )

    op.create_table(
        "glacier_messages",
        # --- Group A: every messages column, same name, same type ---
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("folder_id", sa.Uuid, nullable=False),
        sa.Column("imap_uid", sa.BigInteger, nullable=True),
        sa.Column("thread_id", sa.Uuid, nullable=False),
        sa.Column("message_id", sa.Text, nullable=True),
        sa.Column("subject", sa.Text, nullable=True),
        sa.Column("from_addr", sa.Text, nullable=True),
        sa.Column("to_addrs", postgresql.JSONB, nullable=True),
        sa.Column("cc_addrs", postgresql.JSONB, nullable=True),
        sa.Column("bcc_addrs", postgresql.JSONB, nullable=True),
        sa.Column("reply_to", sa.Text, nullable=True),
        sa.Column("in_reply_to", sa.Text, nullable=True),
        sa.Column("references", sa.ARRAY(sa.Text), nullable=True),
        sa.Column("body_text", sa.Text, nullable=True),
        sa.Column("body_html", sa.Text, nullable=True),
        sa.Column("raw_headers", postgresql.JSONB, nullable=True),
        sa.Column("raw_source", sa.LargeBinary, nullable=True),
        sa.Column("is_truncated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("modseq", sa.BigInteger, nullable=True),
        sa.Column("is_seen", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_flagged", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_answered", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_draft", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("keywords", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("expunged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        # --- Group B: glacier's own columns ---
        sa.Column("msg_key", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.LargeBinary, nullable=True),
        sa.Column("attachment_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("origin_message_id", sa.Uuid, nullable=True),
        sa.Column("origin_folder_id", sa.Uuid, nullable=True),
        sa.Column("origin_imap_name", sa.Text, nullable=True),
        sa.Column("origin_imap_uid", sa.BigInteger, nullable=True),
        sa.Column("state", sa.Text, nullable=False, server_default="copied"),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("glaciered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expunge_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restore_outbox_id", sa.Uuid, nullable=True),
        sa.Column("restore_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "glacier_created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "glacier_updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('copied', 'verified', 'removing', 'glaciered', 'restoring', "
            "'expunge_failed', 'restore_failed')",
            name="ck_glacier_messages_state",
        ),
    )
    op.create_unique_constraint(
        "uq_glacier_messages_account_msg_key", "glacier_messages", ["account_id", "msg_key"],
    )
    op.execute(
        f"ALTER TABLE glacier_messages "
        f"ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({_SEARCH_VECTOR_EXPR}) STORED"
    )
    op.execute(
        "CREATE INDEX idx_glacier_messages_search_vector "
        "ON glacier_messages USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_glacier_messages_folder_received "
        "ON glacier_messages (folder_id, received_at DESC) WHERE visible_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_glacier_messages_account_thread "
        "ON glacier_messages (account_id, thread_id) WHERE visible_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_glacier_messages_state ON glacier_messages (state) "
        "WHERE state <> 'glaciered'"
    )
    # The typo-tolerant fallback tier's own indexes -- the same shape and
    # the same gin_trgm_ops operator class PostIMAP's messages table
    # carries for subject/from_addr (see its consumer contract's
    # "Trigram indexes" section). pg_trgm is already installed (0019).
    op.execute(
        "CREATE INDEX idx_glacier_messages_trgm_subject "
        "ON glacier_messages USING gin (subject gin_trgm_ops) WHERE visible_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_glacier_messages_trgm_from "
        "ON glacier_messages USING gin (from_addr gin_trgm_ops) WHERE visible_at IS NOT NULL"
    )

    op.create_table(
        "glacier_attachments",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "glacier_message_id", sa.Uuid,
            sa.ForeignKey("glacier_messages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("source_attachment_id", sa.Uuid, nullable=True),
        sa.Column("filename", sa.Text, nullable=True),
        sa.Column("content_type", sa.Text, nullable=True),
        sa.Column("content_id", sa.Text, nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("data", sa.LargeBinary, nullable=True),
    )
    op.create_index(
        "idx_glacier_attachments_message_id", "glacier_attachments", ["glacier_message_id"],
    )


def downgrade() -> None:
    """Drop the glacier's tables and account_prefs columns."""
    op.drop_table("glacier_attachments")
    op.drop_table("glacier_messages")
    op.drop_constraint(
        "uq_account_prefs_glacier_folder_id", "account_prefs", type_="unique",
    )
    op.drop_column("account_prefs", "glacier_auto_days")
    op.drop_column("account_prefs", "glacier_folder_id")
    op.drop_column("account_prefs", "glacier_enabled")
