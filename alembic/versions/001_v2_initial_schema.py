"""MailVerdict tables for PostIMAP integration.

Creates only MailVerdict-owned tables. PostIMAP-owned tables
(accounts, folders, messages, attachments, sync_queue, sync_state, sync_audit)
are created by PostIMAP's Kysely migrations.

Revision ID: 001_v2
Revises:
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_v2"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create MailVerdict-owned tables."""
    op.create_table(
        "settings",
        sa.Column("category", sa.String(100), primary_key=True),
        sa.Column("data", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "account_prefs",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("emoji", sa.String(10), nullable=True),
        sa.Column("spam_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("embedding_lookback_days", sa.Integer, nullable=False, server_default="30"),
        sa.Column("folder_mapping", postgresql.JSONB, nullable=True),
        sa.Column("folder_order", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "folder_prefs",
        sa.Column("folder_id", sa.Uuid, sa.ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("unified_name", sa.String(255), nullable=True),
        sa.Column("is_visible", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("subscribed", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("display_name", sa.String(255), nullable=True),
    )

    op.create_table(
        "verdicts",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mail_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_spam", sa.Boolean, nullable=False),
        sa.Column("model_used", sa.String(255), nullable=True),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("neighbor_ids", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_verdict_mail_id", "verdicts", ["mail_id"])

    op.create_table(
        "mail_tags",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mail_id", sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_mail_tag", "mail_tags", ["mail_id", "tag_name"])
    op.create_index("idx_mail_tag_mail_id", "mail_tags", ["mail_id"])

    op.create_table(
        "image_exceptions",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exception_type", sa.String(20), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_image_exception", "image_exceptions", ["account_id", "exception_type", "value"])
    op.create_index("idx_image_exception_account", "image_exceptions", ["account_id"])


def downgrade() -> None:
    """Drop MailVerdict-owned tables."""
    op.drop_table("image_exceptions")
    op.drop_table("mail_tags")
    op.drop_table("verdicts")
    op.drop_table("folder_prefs")
    op.drop_table("account_prefs")
    op.drop_table("settings")
