"""Unified views as their own rows: a folder can belong to several, and a
view carries an icon.

A folder used to carry at most one unified name in folder_prefs, and a
view existed only as long as some folder named it, so one folder could
never sit in two views and there was nowhere to keep a view's own icon.
unified_views holds the views (name, emoji, sidebar position) and
unified_view_folders the many-to-many membership. The membership's
folder_id is a plain UUID with no foreign key onto PostIMAP's folders --
see docs/architecture.md, "Owned tables carry no foreign keys onto
PostIMAP's".

The sidebar order moves from the settings row's folder_order list into
unified_views.position, so the order has one home.

Revision ID: 0030_unified_views
Revises: 0029_alerts_unresolved_mail
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0030_unified_views"
down_revision: str | None = "0029_alerts_unresolved_mail"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create both tables, backfill them from folder_prefs.unified_name and
    the stored order, then drop the old column and order row."""
    op.create_table(
        "unified_views",
        sa.Column(
            "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("emoji", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("name", name="uq_unified_views_name"),
    )
    op.create_table(
        "unified_view_folders",
        sa.Column(
            "view_id", sa.Uuid(),
            sa.ForeignKey("unified_views.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("folder_id", sa.Uuid(), primary_key=True),
    )
    op.create_index(
        "idx_unified_view_folders_folder_id", "unified_view_folders", ["folder_id"],
    )

    # A name listed twice in the stored order keeps its first position; a
    # name the order never listed goes after every listed one,
    # alphabetically -- the same order the sidebar showed before.
    op.execute(
        """
        WITH names AS (
            SELECT DISTINCT unified_name AS name FROM folder_prefs
            WHERE unified_name IS NOT NULL AND unified_name <> ''
        ),
        stored AS (
            SELECT entry.value AS name, min(entry.ordinality) AS pos
            FROM settings,
                 jsonb_array_elements_text(
                     COALESCE(settings.data -> 'folder_order', '[]'::jsonb)
                 ) WITH ORDINALITY AS entry(value, ordinality)
            WHERE settings.category = 'unified_view'
            GROUP BY entry.value
        )
        INSERT INTO unified_views (name, position)
        SELECT names.name,
               (row_number() OVER (ORDER BY stored.pos NULLS LAST, names.name)) - 1
        FROM names LEFT JOIN stored ON stored.name = names.name
        """
    )
    op.execute(
        """
        INSERT INTO unified_view_folders (view_id, folder_id)
        SELECT unified_views.id, folder_prefs.folder_id
        FROM folder_prefs JOIN unified_views ON unified_views.name = folder_prefs.unified_name
        """
    )
    op.drop_column("folder_prefs", "unified_name")
    op.execute("DELETE FROM settings WHERE category = 'unified_view'")


def downgrade() -> None:
    """Restore folder_prefs.unified_name and the stored order. A folder in
    several views keeps only the first by position -- the old shape cannot
    say more -- and a view's emoji is lost."""
    op.add_column("folder_prefs", sa.Column("unified_name", sa.String(255), nullable=True))
    op.execute(
        """
        INSERT INTO folder_prefs (folder_id, is_visible, unified_name)
        SELECT DISTINCT ON (m.folder_id) m.folder_id, true, v.name
        FROM unified_view_folders m JOIN unified_views v ON v.id = m.view_id
        ORDER BY m.folder_id, v.position, v.name
        ON CONFLICT (folder_id) DO UPDATE SET unified_name = EXCLUDED.unified_name
        """
    )
    op.execute(
        """
        INSERT INTO settings (category, data)
        SELECT 'unified_view',
               jsonb_build_object(
                   'folder_order', COALESCE(jsonb_agg(name ORDER BY position, name), '[]'::jsonb)
               )
        FROM unified_views
        HAVING count(*) > 0
        """
    )
    op.drop_index("idx_unified_view_folders_folder_id", table_name="unified_view_folders")
    op.drop_table("unified_view_folders")
    op.drop_table("unified_views")
