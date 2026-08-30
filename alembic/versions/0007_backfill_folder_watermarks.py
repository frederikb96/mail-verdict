"""Give every already-synced folder a watermark.

The watermark says when a folder's first sync finished, which is what
separates historical mail from mail that arrived live. It is written when
PostIMAP reports a folder's initial sync complete -- an event that fires
exactly once per folder, and only ever again for a folder that has not
synced yet.

So a folder that finished syncing before this table existed never gets one.
Nothing errors: mail arrives, is embedded, and is then found ineligible and
never classified, silently and forever.

Backfilling at the current time rather than at the folder's own completion
is deliberate. It makes everything already in the database historical, which
preserves the guarantee that existing mail is never classified in bulk, and
makes everything arriving afterwards live.

Revision ID: 0007_backfill_folder_watermarks
Revises: 0006_pipeline
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_backfill_folder_watermarks"
down_revision: str | None = "0006_pipeline"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Insert a watermark for every folder whose first sync already finished."""
    op.execute(
        """
        INSERT INTO pipeline_folder_state (folder_id, account_id, backfill_completed_at)
        SELECT f.id, f.account_id, now()
        FROM folders f
        WHERE f.initial_sync_done
          AND f.deleted_at IS NULL
        ON CONFLICT (folder_id) DO NOTHING
        """
    )


def downgrade() -> None:
    """Nothing to undo -- a watermark is a fact about a folder, not a schema."""
