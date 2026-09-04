"""Enable pg_trgm: fuzzy, typo-tolerant matching for search.

Unlike `vector` (0005), pg_trgm is a trusted extension -- any role holding
CREATE on the database can install it, no superuser step required, and it
ships with every stock Postgres image rather than needing a
pgvector-style variant. Still wrapped in a try/except with an actionable
message, on the same reasoning 0005 gives: a schema that silently
degrades without it is worse than a migration that refuses to run.

This only registers the extension. It does not index anything -- the
`messages` table this powers fuzzy search over is PostIMAP's, not
MailVerdict's, so an index there is not this migration's call to make.

Revision ID: 0018_pg_trgm
Revises: 0017_heal_stranded_intake
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic import op

revision = "0018_pg_trgm"
down_revision = "0017_heal_stranded_intake"
branch_labels = None
depends_on = None

_EXTENSION_ERROR = (
    "The 'pg_trgm' extension is not installed and this role cannot install "
    "it. pg_trgm is trusted (no superuser needed since PostgreSQL 13), so "
    "this is almost certainly a role missing plain CREATE on the database "
    "rather than a superuser requirement -- grant that, or as a superuser:\n\n"
    "    CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
)


def upgrade() -> None:
    """Register pg_trgm."""
    bind = op.get_bind()
    try:
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    except DBAPIError as exc:
        raise RuntimeError(_EXTENSION_ERROR) from exc


def downgrade() -> None:
    """Leave pg_trgm in place -- another table or a later migration may
    still depend on it, and dropping an extension is not this migration's
    call to make."""
