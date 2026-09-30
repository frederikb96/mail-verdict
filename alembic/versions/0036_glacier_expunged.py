"""Give a permanent glacier delete a tombstone, the same shape restore already has.

A glacier row's own permanent-delete action used to remove the row
outright. That left the message readable forever under the id it had
before it was glaciered: a plain expunge (no glacier involved) leaves
messages with expunged_at set and its bytes intact deliberately, so an
ordinary client's own move to another folder still opens under the old
id -- and with the glacier row physically gone, that same fallback
served the stale, "destroyed on purpose" row as if it were merely
moved. 'expunged' distinguishes the two: a tombstone the read paths can
recognise and refuse, rather than nothing at all to recognise.

glacier_messages is MailVerdict-owned (alembic/versions/0034_glacier.py),
so this is an ordinary local migration.

Revision ID: 0036_glacier_expunged
Revises: 0035_glacier_restored
"""

from __future__ import annotations

from alembic import op

revision: str = "0036_glacier_expunged"
down_revision: str | None = "0035_glacier_restored"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_glacier_messages_state", "glacier_messages", type_="check")
    op.create_check_constraint(
        "ck_glacier_messages_state",
        "glacier_messages",
        "state IN ('copied', 'verified', 'removing', 'glaciered', 'restoring', "
        "'expunge_failed', 'restore_failed', 'restored', 'expunged')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_glacier_messages_state", "glacier_messages", type_="check")
    op.create_check_constraint(
        "ck_glacier_messages_state",
        "glacier_messages",
        "state IN ('copied', 'verified', 'removing', 'glaciered', 'restoring', "
        "'expunge_failed', 'restore_failed', 'restored')",
    )
