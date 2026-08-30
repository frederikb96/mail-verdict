"""MailVerdict v1 baseline -- owned tables only.

PostIMAP-owned tables (accounts, folders, messages, attachments, sync_state,
sync_audit, sync_queue, outbox, outbox_attachments, postimap_info) are
created by PostIMAP's own migrations, not here. None of the tables in this
migration carry a foreign key onto a PostIMAP-owned table: the consumer
database role has no REFERENCES grant on them, and PostIMAP's retention
purge of expunged messages must not cascade away verdict history. See
database/models.py for the full rationale, repeated per table there.

Assumes an empty database -- there is no upgrade path from the pre-contract
schema. PostIMAP re-syncs from IMAP on a fresh install, so re-initializing
is the correct and only supported path (see the PostIMAP handover notes).

Revision ID: 0001_v1_baseline
Revises:
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_v1_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create MailVerdict-owned tables."""
    op.create_table(
        "settings",
        sa.Column("category", sa.String(100), primary_key=True),
        sa.Column("data", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "account_prefs",
        sa.Column("account_id", sa.Uuid, primary_key=True),
        sa.Column("emoji", sa.String(10), nullable=True),
        sa.Column("spam_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("folder_order", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "folder_prefs",
        sa.Column("folder_id", sa.Uuid, primary_key=True),
        sa.Column("is_visible", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("unified_name", sa.String(255), nullable=True),
        sa.Column("special_use_override", sa.Text, nullable=True),
        sa.CheckConstraint(
            "special_use_override IN "
            "('inbox', 'sent', 'drafts', 'trash', 'junk', 'archive', 'all', 'flagged')",
            name="ck_folder_prefs_special_use_override",
        ),
    )

    op.create_table(
        "verdicts",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mail_id", sa.Uuid, nullable=False),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("message_id_hdr", sa.Text, nullable=True),
        sa.Column("is_spam", sa.Boolean, nullable=False),
        sa.Column("model_used", sa.String(255), nullable=True),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_verdict_mail_id", "verdicts", ["mail_id"])
    op.create_index("idx_verdict_account_id", "verdicts", ["account_id"])
    op.create_index(
        "uq_verdict_ai_account_message_hdr",
        "verdicts",
        ["account_id", "message_id_hdr"],
        unique=True,
        postgresql_where=sa.text("source = 'ai' AND message_id_hdr IS NOT NULL"),
    )

    op.create_table(
        "mail_tags",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mail_id", sa.Uuid, nullable=False),
        sa.Column("tag_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint("uq_mail_tag", "mail_tags", ["mail_id", "tag_name"])
    op.create_index("idx_mail_tag_mail_id", "mail_tags", ["mail_id"])

    op.create_table(
        "image_exceptions",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("exception_type", sa.String(20), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_image_exception", "image_exceptions", ["account_id", "exception_type", "value"],
    )
    op.create_index("idx_image_exception_account", "image_exceptions", ["account_id"])


def downgrade() -> None:
    """Drop MailVerdict-owned tables."""
    op.drop_table("image_exceptions")
    op.drop_table("mail_tags")
    op.drop_table("verdicts")
    op.drop_table("folder_prefs")
    op.drop_table("account_prefs")
    op.drop_table("settings")
