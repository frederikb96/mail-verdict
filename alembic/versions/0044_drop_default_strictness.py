"""Drop the semantic search strictness setting.

Semantic search no longer has a strictness level (results are tiered
relative to each query's own score spread), so the stored
semantic.default_strictness value has nothing left to configure. Leaving
it in the row would keep listing it as a setting.

Revision ID: 0044_drop_default_strictness
Revises: 0043_order_controls
"""

from __future__ import annotations

from alembic import op

revision: str = "0044_drop_default_strictness"
down_revision: str | None = "0043_order_controls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE settings SET data = data - 'default_strictness' "
        "WHERE category = 'semantic' AND data ? 'default_strictness'"
    )


def downgrade() -> None:
    """No-op: the removed value configured nothing a downgrade could restore
    meaningfully, and the older code falls back to its own default."""
