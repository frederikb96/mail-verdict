"""Surface why the automatic glacier sweep skipped an account.

_sweep_guard_reason's answer was logged at debug and nowhere else --
some of the guards it checks never self-clear on their own (an
unacknowledged sync failure sits there until someone acknowledges it),
so a person who sets the days and sees nothing happen had no way to
find out why. account_prefs.glacier_sweep_last_refusal is the durable
record the account API and the account page read; the sweep clears it
the moment a tick actually proceeds.

account_prefs is MailVerdict-owned (alembic/versions/0001 or wherever
it was created), so this is an ordinary local migration.

Revision ID: 0037_glacier_sweep_refusal
Revises: 0036_glacier_expunged
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0037_glacier_sweep_refusal"
down_revision: str | None = "0036_glacier_expunged"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "account_prefs", sa.Column("glacier_sweep_last_refusal", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("account_prefs", "glacier_sweep_last_refusal")
