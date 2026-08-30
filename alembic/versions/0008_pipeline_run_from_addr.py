"""Add from_addr to pipeline_runs, and fold it into the dedup index.

The run-level dedup key was (account_id, msg_key, dedup_key), with no
sender -- while verdicts' own AI-verdict index deliberately includes
from_addr (see 0002_verdict_msg_key) so that a message forging the
Message-ID of one already verdicted gets its own verdict rather than
inheriting the existing one. Because a run's dedup collapses before
`classify` ever reaches that check, the protection was unreachable: a
second message reusing a Message-ID produced no run at all -- never
embedded into a verdict, never classified -- and the ON CONFLICT DO
UPDATE repointed the first message's run row at the second message's
id, so the first message's own run history disappeared from underneath
it.

Folding from_addr into the run's own dedup index, the same way it is
folded into the verdict index, means two different senders reusing one
Message-ID each get their own run.

Revision ID: 0008_pipeline_run_from_addr
Revises: 0007_backfill_folder_watermarks
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0008_pipeline_run_from_addr"
down_revision: str | None = "0007_backfill_folder_watermarks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the column, then replace the plain unique constraint with an
    expression index that folds it in the same way verdicts does."""
    op.add_column("pipeline_runs", sa.Column("from_addr", sa.Text, nullable=True))
    op.drop_constraint("uq_pipeline_run", "pipeline_runs", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_pipeline_run ON pipeline_runs "
        "(account_id, msg_key, dedup_key, coalesce(from_addr, ''))"
    )


def downgrade() -> None:
    """Restore the plain unique constraint and drop the column."""
    op.execute("DROP INDEX uq_pipeline_run")
    op.create_unique_constraint(
        "uq_pipeline_run", "pipeline_runs", ["account_id", "msg_key", "dedup_key"],
    )
    op.drop_column("pipeline_runs", "from_addr")
