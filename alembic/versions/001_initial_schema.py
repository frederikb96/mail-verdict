"""Initial schema: accounts, folders, mails, attachments, verdicts, mail_tags.

Revision ID: 001
Revises:
Create Date: 2026-03-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from alembic import op

revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # accounts
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("imap_host", sa.String(255), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False),
        sa.Column("imap_user", sa.String(255), nullable=False),
        sa.Column("smtp_host", sa.String(255), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_user", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # folders
    op.create_table(
        "folders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("imap_name", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("special_use", sa.String(50), nullable=True),
        sa.Column("separator", sa.String(5), nullable=True),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("uidnext", sa.BigInteger(), nullable=True),
        sa.Column("highestmodseq", sa.BigInteger(), nullable=True),
        sa.Column("subscribed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("flags", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("account_id", "imap_name", name="uq_folder_account_imap_name"),
    )
    op.create_index("idx_folder_account_id", "folders", ["account_id"])

    # mails
    op.create_table(
        "mails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=False),
        sa.Column("uid", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.String(998), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("from_addr", sa.String(512), nullable=True),
        sa.Column("to_addrs", JSONB(), nullable=True),
        sa.Column("cc_addrs", JSONB(), nullable=True),
        sa.Column("bcc_addrs", JSONB(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("raw_headers", JSONB(), nullable=True),
        sa.Column("raw_source", sa.LargeBinary(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("modseq", sa.BigInteger(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dkim_pass", sa.Boolean(), nullable=True),
        sa.Column("spf_pass", sa.Boolean(), nullable=True),
        sa.Column("dmarc_pass", sa.Boolean(), nullable=True),
        sa.Column("search_vector", TSVECTOR(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"], ["folders.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("folder_id", "uid", name="uq_mail_folder_uid"),
    )
    op.create_index("idx_mail_folder_uid", "mails", ["folder_id", "uid"])
    op.create_index("idx_mail_message_id", "mails", ["message_id"])
    op.create_index(
        "idx_mail_received_at", "mails", [sa.text("received_at DESC")]
    )
    op.create_index(
        "idx_mail_search_vector",
        "mails",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index("idx_mail_account_id", "mails", ["account_id"])

    # tsvector auto-update trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION mails_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.subject, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.body_text, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_mails_search_vector
        BEFORE INSERT OR UPDATE OF subject, body_text ON mails
        FOR EACH ROW EXECUTE FUNCTION mails_search_vector_update();
    """)

    # pg_trgm indexes for fuzzy search
    op.execute(
        "CREATE INDEX idx_mail_subject_trgm ON mails USING gin (subject gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_mail_body_text_trgm ON mails USING gin (body_text gin_trgm_ops)"
    )

    # attachments
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mail_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("content_id", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["mail_id"], ["mails.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("idx_attachment_mail_id", "attachments", ["mail_id"])

    # verdicts
    op.create_table(
        "verdicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mail_id", sa.Uuid(), nullable=False),
        sa.Column("is_spam", sa.Boolean(), nullable=False),
        sa.Column("model_used", sa.String(255), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("neighbor_ids", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["mail_id"], ["mails.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("idx_verdict_mail_id", "verdicts", ["mail_id"])

    # mail_tags
    op.create_table(
        "mail_tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mail_id", sa.Uuid(), nullable=False),
        sa.Column("tag_name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["mail_id"], ["mails.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("mail_id", "tag_name", name="uq_mail_tag"),
    )
    op.create_index("idx_mail_tag_mail_id", "mail_tags", ["mail_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("mail_tags")
    op.drop_table("verdicts")
    op.drop_table("attachments")

    op.execute("DROP TRIGGER IF EXISTS trg_mails_search_vector ON mails")
    op.execute("DROP FUNCTION IF EXISTS mails_search_vector_update()")
    op.execute("DROP INDEX IF EXISTS idx_mail_body_text_trgm")
    op.execute("DROP INDEX IF EXISTS idx_mail_subject_trgm")
    op.drop_table("mails")

    op.drop_table("folders")
    op.drop_table("accounts")

    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
