"""A folder can be taken out of the pipeline and the semantic index.

folder_prefs gains pipeline_enabled (NULL leaves the decision to the
folder's role). Gmail accounts already mirrored get the defaults the
running service applies to a new folder: a folder holding everything
(role `all`) becomes the Archive, and every folder that is not one of
Gmail's own-mail roles -- labels, Starred, Important -- is switched off.

Revision ID: 0052_folder_pipeline_switch
Revises: 0051_order_files_owner
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0052_folder_pipeline_switch"
down_revision: str | None = "0051_order_files_owner"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("folder_prefs", sa.Column("pipeline_enabled", sa.Boolean(), nullable=True))
    op.execute(
        """
        INSERT INTO folder_prefs (folder_id, special_use_override, pipeline_enabled)
        SELECT f.id,
               CASE WHEN f.special_use = 'all' THEN 'archive' END,
               CASE WHEN a.imap_host IN ('imap.gmail.com', 'imap.googlemail.com')
                         AND coalesce(f.special_use, '') NOT IN
                             ('inbox', 'sent', 'drafts', 'trash', 'junk', 'archive', 'all')
                    THEN false END
        FROM folders f
        JOIN accounts a ON a.id = f.account_id
        WHERE f.deleted_at IS NULL
          AND (f.special_use = 'all'
               OR (a.imap_host IN ('imap.gmail.com', 'imap.googlemail.com')
                   AND coalesce(f.special_use, '') NOT IN
                       ('inbox', 'sent', 'drafts', 'trash', 'junk', 'archive', 'all')))
        ON CONFLICT (folder_id) DO UPDATE SET
            special_use_override = coalesce(
                folder_prefs.special_use_override, EXCLUDED.special_use_override),
            pipeline_enabled = coalesce(folder_prefs.pipeline_enabled, EXCLUDED.pipeline_enabled)
        """
    )


def downgrade() -> None:
    op.drop_column("folder_prefs", "pipeline_enabled")
