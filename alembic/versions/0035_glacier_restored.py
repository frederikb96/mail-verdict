"""Give a completed restore a terminal state.

confirm_restores set restored_at and cleared the bulk columns but never
changed state away from 'restoring', so a completed restore re-confirmed
(and re-announced) on every sweep tick, and the restore timeout could
still mark a restore that finished minutes earlier as failed. 'restored'
closes that: the tombstone shape (restored_at set, raw_source NULL) now
also carries its own terminal state.

glacier_messages is MailVerdict-owned (alembic/versions/0034_glacier.py),
so this is an ordinary local migration, not an upstream request.

Revision ID: 0035_glacier_restored
Revises: 0034_glacier
"""

from __future__ import annotations

from alembic import op

revision: str = "0035_glacier_restored"
down_revision: str | None = "0034_glacier"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_glacier_messages_state", "glacier_messages", type_="check")
    op.create_check_constraint(
        "ck_glacier_messages_state",
        "glacier_messages",
        "state IN ('copied', 'verified', 'removing', 'glaciered', 'restoring', "
        "'expunge_failed', 'restore_failed', 'restored')",
    )
    # Heal any row a pre-fix confirm_restores already left in the old,
    # non-terminal shape -- restored_at set (confirmed), state still
    # 'restoring' (never advanced). No production data exists for this
    # unreleased feature; harmless and correct either way.
    op.execute(
        "UPDATE glacier_messages SET state = 'restored' "
        "WHERE state = 'restoring' AND restored_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE glacier_messages SET state = 'restoring' WHERE state = 'restored'"
    )
    op.drop_constraint("ck_glacier_messages_state", "glacier_messages", type_="check")
    op.create_check_constraint(
        "ck_glacier_messages_state",
        "glacier_messages",
        "state IN ('copied', 'verified', 'removing', 'glaciered', 'restoring', "
        "'expunge_failed', 'restore_failed')",
    )
