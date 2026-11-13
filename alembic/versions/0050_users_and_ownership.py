"""Users, and who owns what.

users and user_identities hold who may sign in (one user, several token
subjects); account_owners and dav_account_owners map every mail and DAV
account to its user, with no foreign key onto PostIMAP's tables; user_settings
holds the settings a user sets for themselves.

Every table holding something a user owns without an account key gains a
user_id: push_subscriptions, alerts, unified_views, orders,
pipeline_revisions, message_action_submissions, outbox_submissions and
llm_usage. The column is nullable: alerts and llm_usage carry rows that
belong to nobody, and code writing the others stamps it as it learns who it
writes for. calendar_links_revision becomes one counter per user, and a
unified view's name is unique per user rather than instance-wide.

Before this revision the instance had one user. Every existing row is given to
the owner row created here (OWNER_USER_ID in auth/principal.py), which the
subjects in auth.owner_subjects resolve to: every mail and DAV account, every
row above except a provider_unavailable alert (a condition of the instance,
seen by admins), and the per-user settings -- the mail, calendar and outbox
categories, the user-level keys of orders, and the account order -- copied
into user_settings.

Revision ID: 0050_users_and_ownership
Revises: 0049_llm_usage
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050_users_and_ownership"
down_revision: str | None = "0049_llm_usage"
branch_labels: str | None = None
depends_on: str | None = None

_OWNER = "00000000-0000-0000-0000-000000000001"

# Owned tables that gain a user_id, each backfilled to the owner except where
# a row belongs to nobody (the WHERE clause, if any).
_USER_COLUMNS: dict[str, str | None] = {
    "push_subscriptions": None,
    "alerts": "kind <> 'provider_unavailable'",
    "unified_views": None,
    "orders": None,
    "pipeline_revisions": None,
    "message_action_submissions": None,
    "outbox_submissions": None,
    "llm_usage": None,
}

# The keys of the orders category a user sets for themselves; the rest of it
# (the model tasks and the worker's tuning) stays instance-wide.
_ORDERS_USER_KEYS = ("language", "filter", "auto_close_days", "auto_close_grace_days")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
    )
    op.create_table(
        "user_identities",
        sa.Column("subject", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_table(
        "account_owners",
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
    )
    op.create_index("ix_account_owners_user_id", "account_owners", ["user_id"])
    op.create_table(
        "dav_account_owners",
        sa.Column("dav_account_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
    )
    op.create_index("ix_dav_account_owners_user_id", "dav_account_owners", ["user_id"])
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("category", sa.String(100), primary_key=True),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )

    op.execute(
        f"INSERT INTO users (id, display_name, role) VALUES ('{_OWNER}', 'Owner', 'admin')"
    )
    op.execute(
        f"INSERT INTO account_owners (account_id, user_id) SELECT id, '{_OWNER}' FROM accounts"
    )
    # dav_accounts exists from PostIMAP 1.6 on; an older PostIMAP has no DAV
    # accounts to own.
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT to_regclass('dav_accounts')")) is not None:
        op.execute(
            "INSERT INTO dav_account_owners (dav_account_id, user_id) "
            f"SELECT id, '{_OWNER}' FROM dav_accounts"
        )

    for table, belongs_to_nobody_unless in _USER_COLUMNS.items():
        op.add_column(
            table, sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        )
        where = f" WHERE {belongs_to_nobody_unless}" if belongs_to_nobody_unless else ""
        op.execute(f"UPDATE {table} SET user_id = '{_OWNER}'{where}")
    for table in ("push_subscriptions", "alerts", "unified_views", "orders", "llm_usage"):
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_index(
        "ix_pipeline_revisions_user_revision", "pipeline_revisions", ["user_id", "revision"],
    )

    op.drop_constraint("uq_unified_views_name", "unified_views", type_="unique")
    # NULLS NOT DISTINCT: a view written before its writer stamps a user
    # stays unique by name among the other unowned ones, as before.
    op.create_unique_constraint(
        "uq_unified_views_user_name", "unified_views", ["user_id", "name"],
        postgresql_nulls_not_distinct=True,
    )

    current = bind.scalar(sa.text("SELECT revision FROM calendar_links_revision LIMIT 1"))
    op.drop_table("calendar_links_revision")
    op.create_table(
        "calendar_links_revision",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        sa.text(
            "INSERT INTO calendar_links_revision (user_id, revision) "
            f"VALUES ('{_OWNER}', :revision)"
        ).bindparams(revision=current or 0)
    )

    op.execute(
        "INSERT INTO user_settings (user_id, category, data) "
        f"SELECT '{_OWNER}', category, data FROM settings "
        "WHERE category IN ('mail', 'calendar', 'outbox')"
    )
    keys = ", ".join(f"'{key}'" for key in _ORDERS_USER_KEYS)
    op.execute(
        "INSERT INTO user_settings (user_id, category, data) "
        f"SELECT '{_OWNER}', 'orders', picked.data FROM ("
        "  SELECT jsonb_object_agg(entry.key, entry.value) AS data "
        "  FROM settings, jsonb_each(settings.data) AS entry "
        f"  WHERE settings.category = 'orders' AND entry.key IN ({keys})"
        ") AS picked WHERE picked.data IS NOT NULL"
    )
    op.execute(
        "INSERT INTO user_settings (user_id, category, data) "
        f"SELECT '{_OWNER}', 'mail', jsonb_build_object('account_order', data->'order') "
        "FROM settings WHERE category = 'account_order' AND data ? 'order' "
        "ON CONFLICT (user_id, category) "
        "DO UPDATE SET data = user_settings.data || excluded.data"
    )


def downgrade() -> None:
    bind = op.get_bind()
    current = bind.scalar(
        sa.text(f"SELECT revision FROM calendar_links_revision WHERE user_id = '{_OWNER}'")
    )
    op.drop_table("calendar_links_revision")
    op.create_table(
        "calendar_links_revision",
        sa.Column("singleton", sa.Boolean, primary_key=True, server_default="true"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("singleton", name="ck_calendar_links_revision_singleton"),
    )
    op.execute(
        sa.text("INSERT INTO calendar_links_revision (revision) VALUES (:revision)").bindparams(
            revision=current or 0,
        )
    )

    # Other users' views would collide on the instance-wide name; only the
    # owner's survive a downgrade.
    op.execute(f"DELETE FROM unified_views WHERE user_id IS DISTINCT FROM '{_OWNER}'")
    op.drop_constraint("uq_unified_views_user_name", "unified_views", type_="unique")
    op.create_unique_constraint("uq_unified_views_name", "unified_views", ["name"])

    op.drop_index("ix_pipeline_revisions_user_revision", table_name="pipeline_revisions")
    for table in ("push_subscriptions", "alerts", "unified_views", "orders", "llm_usage"):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
    for table in _USER_COLUMNS:
        op.drop_column(table, "user_id")

    op.drop_table("user_settings")
    op.drop_index("ix_dav_account_owners_user_id", table_name="dav_account_owners")
    op.drop_table("dav_account_owners")
    op.drop_index("ix_account_owners_user_id", table_name="account_owners")
    op.drop_table("account_owners")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_table("users")
