"""Signed-in phones, browsers and machine clients, each revocable on its own.

One row per sign-in (auth/sign_ins.py): a phone by its identity provider
session, a person's browsers together, a machine client by its subject. A
revoked row stays, so the same handle is refused when it comes back.

Revision ID: 0054_sign_ins
Revises: 0053_user_id_not_null
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0054_sign_ins"
down_revision: str | None = "0053_user_id_not_null"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "sign_ins",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('phone', 'browser', 'machine')", name="ck_sign_ins_kind"),
        sa.UniqueConstraint("kind", "handle", name="uq_sign_ins_kind_handle"),
    )
    op.create_index("ix_sign_ins_user_id", "sign_ins", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sign_ins_user_id", table_name="sign_ins")
    op.drop_table("sign_ins")
