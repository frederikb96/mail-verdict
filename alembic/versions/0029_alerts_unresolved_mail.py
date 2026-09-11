"""Index the unresolved mail alerts by the message they announce.

A mail alert resolves itself once its mail is read (alerts/resolve.py),
checked on every message insert or read-state change and by a periodic
sweep. Both look alerts up by message_id among the unresolved mail rows
only, which stay few however large the table grows, since resolving is
exactly what keeps them few.

Revision ID: 0029_alerts_unresolved_mail
Revises: 0028_retention_min_days
"""

from __future__ import annotations

from alembic import op

revision: str = "0029_alerts_unresolved_mail"
down_revision: str | None = "0028_retention_min_days"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the partial index."""
    op.execute(
        "CREATE INDEX idx_alerts_unresolved_mail ON alerts (message_id) "
        "WHERE kind = 'mail' AND dismissed_at IS NULL"
    )


def downgrade() -> None:
    """Drop the partial index."""
    op.execute("DROP INDEX idx_alerts_unresolved_mail")
