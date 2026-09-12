"""Native devices as push_subscriptions rows.

A phone registers through a push relay rather than a browser push service,
so its row carries the relay's address, the sealed ticket the relay issued
for it and the key its notification extension decrypts with -- both stored
encrypted under ENCRYPTION_KEY -- and none of the Web Push endpoint/keys.
transport says which kind a row is; one CHECK per kind keeps each row
carrying exactly what its own sender needs. installation_id is what a
phone re-registers under, unique among the rows that have one.

muted_channels is per-device opt-out ('mail', 'system'), empty for every
existing row so nothing that notifies today stops.

Downgrade deletes the native rows first: the endpoint/keys columns go back
to NOT NULL and a native row has none.

Revision ID: 0032_native_push
Revises: 0031_unified_views
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0032_native_push"
down_revision: str | None = "0031_unified_views"
branch_labels: str | None = None
depends_on: str | None = None

_WEBPUSH_FIELDS = ("endpoint", "p256dh", "auth")


def upgrade() -> None:
    """Add the transport, native and muting columns, their checks and index."""
    op.add_column(
        "push_subscriptions",
        sa.Column("transport", sa.Text, nullable=False, server_default="webpush"),
    )
    for column in _WEBPUSH_FIELDS:
        op.alter_column("push_subscriptions", column, nullable=True)
    op.add_column("push_subscriptions", sa.Column("installation_id", sa.Uuid, nullable=True))
    op.add_column("push_subscriptions", sa.Column("relay_url", sa.Text, nullable=True))
    op.add_column(
        "push_subscriptions", sa.Column("encrypted_relay_ticket", sa.LargeBinary, nullable=True),
    )
    op.add_column(
        "push_subscriptions", sa.Column("encrypted_content_key", sa.LargeBinary, nullable=True),
    )
    op.add_column(
        "push_subscriptions",
        sa.Column(
            "muted_channels", sa.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_push_subscriptions_transport", "push_subscriptions",
        "transport IN ('webpush', 'apns')",
    )
    op.create_check_constraint(
        "ck_push_subscriptions_webpush_fields", "push_subscriptions",
        "transport <> 'webpush' OR "
        "(endpoint IS NOT NULL AND p256dh IS NOT NULL AND auth IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_push_subscriptions_apns_fields", "push_subscriptions",
        "transport <> 'apns' OR (installation_id IS NOT NULL AND relay_url IS NOT NULL "
        "AND encrypted_relay_ticket IS NOT NULL AND encrypted_content_key IS NOT NULL)",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_push_subscriptions_installation_id "
        "ON push_subscriptions (installation_id) WHERE installation_id IS NOT NULL"
    )


def downgrade() -> None:
    """Delete native rows, then drop everything upgrade added."""
    op.execute("DELETE FROM push_subscriptions WHERE transport <> 'webpush'")
    op.execute("DROP INDEX uq_push_subscriptions_installation_id")
    for name in (
        "ck_push_subscriptions_apns_fields",
        "ck_push_subscriptions_webpush_fields",
        "ck_push_subscriptions_transport",
    ):
        op.drop_constraint(name, "push_subscriptions", type_="check")
    for column in (
        "muted_channels", "encrypted_content_key", "encrypted_relay_ticket", "relay_url",
        "installation_id",
    ):
        op.drop_column("push_subscriptions", column)
    for column in _WEBPUSH_FIELDS:
        op.alter_column("push_subscriptions", column, nullable=False)
    op.drop_column("push_subscriptions", "transport")
