"""Drop the (account_id, received_at) btree on message_embeddings.

Nothing reads it: the exact path joins a window's messages through
messages' own indexes, and the vector index query filters received_at inside
the HNSW scan. It costs a write on every finished embedding. The other
indexes 0045 added are used and stay.

Revision ID: 0046_drop_unused_received_index
Revises: 0045_halfvec_embeddings
"""

from __future__ import annotations

from alembic import op

revision: str = "0046_drop_unused_received_index"
down_revision: str | None = "0045_halfvec_embeddings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_message_embeddings_account_received")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_message_embeddings_account_received")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_message_embeddings_account_received "
            "ON message_embeddings (account_id, received_at) WHERE status = 'done'"
        )
