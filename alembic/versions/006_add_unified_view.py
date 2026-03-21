"""Add unified view columns: unified_name on folders, emoji on accounts.

Revision ID: 006
Revises: 005
Create Date: 2026-03-21

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: Union[str, Sequence[str], None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unified_name to folders and emoji to accounts."""
    op.add_column(
        "folders",
        sa.Column("unified_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("emoji", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    """Remove unified view columns."""
    op.drop_column("accounts", "emoji")
    op.drop_column("folders", "unified_name")
