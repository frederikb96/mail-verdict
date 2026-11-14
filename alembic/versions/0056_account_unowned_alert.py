"""The alert kind `account_unowned`: a mail or DAV account nobody owns.

Such an account's mail is never classified and raises no alert, and nobody
sees it; alerts/ownerless.py tells the admins.

Revision ID: 0056_account_unowned_alert
Revises: 0055_native_push_transport
"""

from __future__ import annotations

from alembic import op

revision: str = "0056_account_unowned_alert"
down_revision: str | None = "0055_native_push_transport"
branch_labels: str | None = None
depends_on: str | None = None

_PREVIOUS_ALERT_KINDS = (
    "'mail', 'reminder', 'outbox_stalled', 'glacier_conflict', 'webhook_failed', "
    "'provider_unavailable'"
)
_ALERT_KINDS = f"{_PREVIOUS_ALERT_KINDS}, 'account_unowned'"


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", f"kind IN ({_ALERT_KINDS})")


def downgrade() -> None:
    op.execute("DELETE FROM alerts WHERE kind = 'account_unowned'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", f"kind IN ({_PREVIOUS_ALERT_KINDS})")
