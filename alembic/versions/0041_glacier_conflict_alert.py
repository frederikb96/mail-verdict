"""Admit the glacier_conflict alert kind.

The automatic glacier sweep's duplicate_conflict outcome (two different
messages sharing one Message-ID header, glacier/operations.py's own
_handle_existing_glacier_row) is a permanent, never-self-clearing state:
until a person acts, the message is reselected as a candidate and refused
again on every tick. Before this, the sweep discarded that outcome with a
bare `continue` -- no log line, no notification, nothing a person could
ever find. glacier/sweep.py now raises a durable, deduplicated alert of
this kind instead, following the same ck_alerts_kind widening
0030_outbox_submissions already did for outbox_stalled.

Revision ID: 0041_glacier_conflict_alert
Revises: 0040_scrub_leaked_provider_keys
"""

from __future__ import annotations

from alembic import op

revision: str = "0041_glacier_conflict_alert"
down_revision: str | None = "0040_scrub_leaked_provider_keys"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind", "alerts",
        "kind IN ('mail', 'reminder', 'outbox_stalled', 'glacier_conflict')",
    )


def downgrade() -> None:
    """Drop every glacier_conflict alert with the kind, then narrow the
    constraint back."""
    op.execute("DELETE FROM alerts WHERE kind = 'glacier_conflict'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind", "alerts", "kind IN ('mail', 'reminder', 'outbox_stalled')",
    )
