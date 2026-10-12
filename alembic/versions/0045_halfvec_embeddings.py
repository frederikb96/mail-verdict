"""Store message embeddings as halfvec, with the message's received_at beside them.

message_embeddings.embedding becomes halfvec(1536): half the storage, and an
HNSW index half the size, which is what keeps the vectors in memory. The HNSW
index is rebuilt on halfvec_cosine_ops for finished rows.

message_embeddings.received_at is the message's received_at, copied from the
live message (through the join hint) or the glacier row (through msg_key), so
a time window can be applied inside the vector index scan. Rows whose message
cannot be found stay NULL; they are unreachable by search anyway.

Also adds the btrees the exact scan over a narrow window needs: the join hint
on message_embeddings, and (account_id, received_at) on message_embeddings
and glacier_messages.

The rewrite of the column holds an exclusive lock on the table for its
duration, so it is committed on its own, and the index is built outside any
transaction with maintenance_work_mem raised so the graph stays in memory.

Revision ID: 0045_halfvec_embeddings
Revises: 0044_drop_default_strictness
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0045_halfvec_embeddings"
down_revision: str | None = "0044_drop_default_strictness"
branch_labels: str | None = None
depends_on: str | None = None

_DIMENSIONS = 1536
_INDEX_MEMORY = "512MB"


def _embedding_type() -> str:
    """The column's current type as Postgres prints it, e.g. 'halfvec(1536)'."""
    return str(op.get_bind().execute(sa.text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'message_embeddings'::regclass AND attname = 'embedding'"
    )).scalar_one())


def _create_index(name: str, definition: str) -> None:
    """Build an index outside a transaction, so writes to the table (the
    embedding queue) are not blocked while it builds. A leftover invalid index
    of the same name from an interrupted build is dropped first."""
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def _build_hnsw(operator_class: str) -> None:
    """The finished-rows HNSW index, with maintenance_work_mem raised so the
    graph stays in memory while it builds."""
    with op.get_context().autocommit_block():
        op.execute(f"SET maintenance_work_mem = '{_INDEX_MEMORY}'")
        _create_index(
            "ix_message_embeddings_hnsw",
            f"message_embeddings USING hnsw (embedding {operator_class}) "
            "WITH (m = 16, ef_construction = 64) WHERE status = 'done'",
        )
        op.execute("RESET maintenance_work_mem")


def upgrade() -> None:
    op.execute("ALTER TABLE message_embeddings ADD COLUMN IF NOT EXISTS received_at timestamptz")
    op.execute(
        "UPDATE message_embeddings me SET received_at = m.received_at "
        "FROM messages m WHERE me.account_id = m.account_id AND me.message_id = m.id"
    )
    op.execute(
        "UPDATE message_embeddings me SET received_at = g.received_at "
        "FROM glacier_messages g WHERE me.received_at IS NULL AND me.message_id IS NULL "
        "AND me.account_id = g.account_id AND me.msg_key = g.msg_key"
    )

    # The old index is on the vector type and cannot follow the column.
    op.execute("DROP INDEX IF EXISTS ix_message_embeddings_hnsw")
    with op.get_context().autocommit_block():
        if _embedding_type() != f"halfvec({_DIMENSIONS})":
            op.execute(
                f"ALTER TABLE message_embeddings ALTER COLUMN embedding "
                f"TYPE halfvec({_DIMENSIONS}) USING embedding::halfvec({_DIMENSIONS})"
            )
    _build_hnsw("halfvec_cosine_ops")

    with op.get_context().autocommit_block():
        _create_index("ix_message_embeddings_message_id", "message_embeddings (message_id)")
        _create_index(
            "ix_message_embeddings_account_received",
            "message_embeddings (account_id, received_at) WHERE status = 'done'",
        )
        _create_index(
            "idx_glacier_messages_account_received",
            "glacier_messages (account_id, received_at) WHERE visible_at IS NOT NULL",
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_glacier_messages_account_received")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_message_embeddings_account_received")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_message_embeddings_message_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_message_embeddings_hnsw")
        if _embedding_type() != f"vector({_DIMENSIONS})":
            op.execute(
                f"ALTER TABLE message_embeddings ALTER COLUMN embedding "
                f"TYPE vector({_DIMENSIONS}) USING embedding::vector({_DIMENSIONS})"
            )
    _build_hnsw("vector_cosine_ops")
    op.execute("ALTER TABLE message_embeddings DROP COLUMN IF EXISTS received_at")
