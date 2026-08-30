"""Verdicts gain a durable msg_key, replacing message_id_hdr as the
durability gate's identity column.

message_id_hdr is kept -- it is still useful for debugging and it is one of
the two inputs msg_key is derived from -- but it is no longer what the
partial unique index constrains. A message with no Message-ID header used
to skip the never-reclassify gate entirely (message_id_hdr NULL was excluded
from the old partial index by construction); msg_key's content-hash fallback
closes that.

from_addr is added alongside it: without it, a sender forging the
Message-ID of a message already verdicted not-spam bypasses classification.
The new index expression coalesces it to '' so that two AI verdicts sharing
a msg_key still conflict even when neither recorded a sender -- Postgres
treats NULL as distinct from itself in a unique index, which would
otherwise silently defeat this for exactly the rows most likely to need it.

Revision ID: 0002_verdict_msg_key
Revises: 0001_v1_baseline
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from mail_verdict.database.msg_key import compute_msg_key

revision = "0002_verdict_msg_key"
down_revision = "0001_v1_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add msg_key/from_addr, backfill, then move the durability index."""
    op.add_column("verdicts", sa.Column("msg_key", sa.Text, nullable=True))
    op.add_column("verdicts", sa.Column("from_addr", sa.Text, nullable=True))

    _backfill_msg_key()

    op.alter_column("verdicts", "msg_key", nullable=False)

    op.drop_index("uq_verdict_ai_account_message_hdr", table_name="verdicts")
    _drop_duplicate_ai_verdicts()
    op.execute(
        "CREATE UNIQUE INDEX uq_verdict_ai_account_msgkey_from "
        "ON verdicts (account_id, msg_key, coalesce(from_addr, '')) "
        "WHERE source = 'ai'"
    )
    op.create_index("idx_verdict_msg_key", "verdicts", ["account_id", "msg_key"])


def _backfill_msg_key() -> None:
    """Compute msg_key for every existing row from its own header, or its
    message's envelope, or -- if the message is already gone -- its id.

    Runs in Python rather than SQL so the hash algorithm has exactly one
    definition, shared with the application code that computes it for new
    rows going forward.
    """
    bind = op.get_bind()

    # msg_key and from_addr are named in the UPDATE below, so they have to
    # be declared here too -- a lightweight sa.table() carries only the
    # columns it is given, and setting one it does not know refuses to
    # compile rather than failing at the database.
    verdicts = sa.table(
        "verdicts",
        sa.column("id", sa.Uuid),
        sa.column("mail_id", sa.Uuid),
        sa.column("account_id", sa.Uuid),
        sa.column("message_id_hdr", sa.Text),
        sa.column("msg_key", sa.Text),
        sa.column("from_addr", sa.Text),
    )
    messages = sa.table(
        "messages",
        sa.column("id", sa.Uuid),
        sa.column("from_addr", sa.Text),
        sa.column("subject", sa.Text),
        sa.column("received_at", sa.DateTime(timezone=True)),
        sa.column("size_bytes", sa.Integer),
    )

    rows = bind.execute(
        sa.select(
            verdicts.c.id,
            verdicts.c.mail_id,
            verdicts.c.account_id,
            verdicts.c.message_id_hdr,
            messages.c.id.label("matched_message_id"),
            messages.c.from_addr,
            messages.c.subject,
            messages.c.received_at,
            messages.c.size_bytes,
        ).select_from(verdicts.outerjoin(messages, verdicts.c.mail_id == messages.c.id))
    ).all()

    for row in rows:
        if row.message_id_hdr:
            msg_key = row.message_id_hdr
            from_addr = row.from_addr
        elif row.matched_message_id is not None:
            msg_key = compute_msg_key(
                account_id=row.account_id,
                message_id_hdr=None,
                from_addr=row.from_addr,
                subject=row.subject,
                received_at=row.received_at,
                size_bytes=row.size_bytes,
            )
            from_addr = row.from_addr
        else:
            msg_key = f"legacy:{row.mail_id}"
            from_addr = None

        bind.execute(
            sa.update(verdicts)
            .where(verdicts.c.id == row.id)
            .values(msg_key=msg_key, from_addr=from_addr)
        )


def _drop_duplicate_ai_verdicts() -> None:
    """Keep the newest AI verdict per (account, msg_key, sender), drop the rest.

    The index about to be built cannot tolerate duplicates, and a database
    carrying the very bug this migration closes is certain to have them: a
    message with no Message-ID header skipped the durability gate, so every
    resync wrote another AI verdict for it, and the old partial index
    excluded NULL headers so nothing refused them. Those rows all derive the
    same msg_key from the same message, so they collide exactly.

    The newest is the one to keep -- it is the classification the
    application last arrived at, and it is what the interface has been
    showing.
    """
    op.execute(
        """
        DELETE FROM verdicts v
        USING verdicts newer
        WHERE v.source = 'ai'
          AND newer.source = 'ai'
          AND v.account_id = newer.account_id
          AND v.msg_key = newer.msg_key
          AND coalesce(v.from_addr, '') = coalesce(newer.from_addr, '')
          AND (v.created_at, v.id) < (newer.created_at, newer.id)
        """
    )


def downgrade() -> None:
    """Drop msg_key/from_addr and the index built on them; restore the
    header-only durability index."""
    op.drop_index("idx_verdict_msg_key", table_name="verdicts")
    op.execute("DROP INDEX uq_verdict_ai_account_msgkey_from")
    op.create_index(
        "uq_verdict_ai_account_message_hdr",
        "verdicts",
        ["account_id", "message_id_hdr"],
        unique=True,
        postgresql_where=sa.text("source = 'ai' AND message_id_hdr IS NOT NULL"),
    )
    op.drop_column("verdicts", "from_addr")
    op.drop_column("verdicts", "msg_key")
