"""Message embeddings: the vector store and its work queue.

message_embeddings carries one row per (account_id, msg_key, model) -- keyed
on the durable message identity (database/msg_key.py), never on messages.id,
so a UIDVALIDITY resync that replaces every row id in a folder cannot orphan
a vector. Retention purge deletes the message row, not this one; the two are
joined explicitly at query time, the same posture every other MailVerdict-
owned table takes toward PostIMAP's.

status/attempts/priority/next_attempt_at/claimed_by/claimed_at/
lease_expires_at/last_error are exactly the column set queue/work_queue.py
requires -- a pending row *is* a queue entry, so there is no second table
that could disagree with it.

The vector extension is a hard startup requirement rather than an optional
capability: `vector` is not a trusted extension, so an application role
cannot install it at runtime, and a schema that silently degrades without it
is worse than one that refuses to run. CREATE EXTENSION is attempted here and
fails with an actionable message naming the exact statement an operator with
superuser needs to run first.

Revision ID: 0005_message_embeddings
Revises: 0004_provider_credentials
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.exc import DBAPIError

from alembic import op
from mail_verdict.database.models import EMBEDDING_DIMENSIONS

revision = "0005_message_embeddings"
down_revision = "0004_provider_credentials"
branch_labels = None
depends_on = None

_EXTENSION_ERROR = (
    "The 'vector' extension is not installed and this role cannot install it "
    "-- pgvector is not a trusted extension, so CREATE EXTENSION requires "
    "superuser. Run the following once, as a superuser, against this "
    "database, then re-run this migration:\n\n"
    "    CREATE EXTENSION IF NOT EXISTS vector;\n\n"
    "The Postgres image must also ship the extension's files -- the stock "
    "postgres image does not; use a pgvector-enabled image "
    "(pgvector/pgvector, or CloudNativePG's pgvector extension image)."
)


def upgrade() -> None:
    """Create the vector extension and message_embeddings."""
    bind = op.get_bind()
    try:
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except DBAPIError as exc:
        raise RuntimeError(_EXTENSION_ERROR) from exc

    op.create_table(
        "message_embeddings",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("msg_key", sa.Text, nullable=False),
        # A join hint only, re-resolved at read time -- never the identity
        # a row is keyed on. See database/msg_key.py.
        sa.Column("message_id", sa.Uuid, nullable=True),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("content_level", sa.Text, nullable=False, server_default="full"),
        sa.Column("source_hash", sa.Text, nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="100"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_by", sa.Text, nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'done', 'failed')",
            name="ck_message_embeddings_status",
        ),
        sa.CheckConstraint(
            "content_level IN ('full', 'envelope')",
            name="ck_message_embeddings_content_level",
        ),
        sa.CheckConstraint(
            "status <> 'done' OR embedding IS NOT NULL",
            name="ck_message_embeddings_done_has_vector",
        ),
        sa.UniqueConstraint(
            "account_id", "msg_key", "model", name="uq_message_embeddings_account_msgkey_model",
        ),
    )
    op.create_index(
        "ix_message_embeddings_claim", "message_embeddings", ["priority", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_message_embeddings_lease", "message_embeddings", ["lease_expires_at"],
        postgresql_where=sa.text("status = 'claimed'"),
    )
    op.create_index(
        "ix_message_embeddings_account", "message_embeddings", ["account_id", "model"],
    )
    op.create_index(
        "ix_message_embeddings_hnsw", "message_embeddings", ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("status = 'done'"),
    )


def downgrade() -> None:
    """Drop message_embeddings. The vector extension itself is left in
    place -- another table or a later migration may still depend on it,
    and dropping an extension a superuser installed is not this
    migration's call to make."""
    op.drop_table("message_embeddings")
