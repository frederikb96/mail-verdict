"""Token usage of paid model calls, and the export's position.

llm_usage holds one row per paid model response (usage/recorder.py);
usage_export_state holds the last row delivered to Elasticsearch and the
outcome of the last export pass (usage/export.py). Both start empty.

Revision ID: 0049_llm_usage
Revises: 0048_task_providers
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0049_llm_usage"
down_revision: str | None = "0048_task_providers"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
    )
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])
    op.create_table(
        "usage_export_state",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("last_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("usage_export_state")
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")
