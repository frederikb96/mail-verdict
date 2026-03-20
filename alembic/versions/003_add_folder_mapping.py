"""Add folder_mapping JSONB to accounts.

Revision ID: 003
Revises: 002
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add folder_mapping column."""
    op.add_column("accounts", sa.Column("folder_mapping", JSONB(), nullable=True))


def downgrade() -> None:
    """Remove folder_mapping column."""
    op.drop_column("accounts", "folder_mapping")
