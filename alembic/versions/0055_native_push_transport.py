"""A phone's push transport is `native`, whatever platform the phone is.

Every phone app -- iPhone or Android -- registers through a push relay
the same way, so its push_subscriptions rows carry `native` rather than the
name of one platform's push service. Existing `apns` rows become `native`.

Revision ID: 0055_native_push_transport
Revises: 0054_sign_ins
"""

from __future__ import annotations

from alembic import op

revision: str = "0055_native_push_transport"
down_revision: str | None = "0054_sign_ins"
branch_labels: str | None = None
depends_on: str | None = None

_RELAY_FIELDS = (
    "(installation_id IS NOT NULL AND relay_url IS NOT NULL "
    "AND encrypted_relay_ticket IS NOT NULL AND encrypted_content_key IS NOT NULL)"
)


def _rename(old: str, new: str) -> None:
    """Move every row and both check constraints from transport `old` to `new`."""
    op.drop_constraint(f"ck_push_subscriptions_{old}_fields", "push_subscriptions", type_="check")
    op.drop_constraint("ck_push_subscriptions_transport", "push_subscriptions", type_="check")
    op.execute(f"UPDATE push_subscriptions SET transport = '{new}' WHERE transport = '{old}'")
    op.create_check_constraint(
        "ck_push_subscriptions_transport", "push_subscriptions",
        f"transport IN ('webpush', '{new}')",
    )
    op.create_check_constraint(
        f"ck_push_subscriptions_{new}_fields", "push_subscriptions",
        f"transport <> '{new}' OR {_RELAY_FIELDS}",
    )


def upgrade() -> None:
    _rename("apns", "native")


def downgrade() -> None:
    _rename("native", "apns")
