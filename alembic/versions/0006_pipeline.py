"""Pipeline core: pipeline_runs, pipeline_revisions, pipeline_folder_state,
and the migration of settings.rules/spam.* into the first pipeline
revision.

pipeline_runs is a work-queue table sharing every column queue/work_queue.py
requires with message_embeddings and queue_state's other consumers, plus the
domain columns that make it the durable "did this message run through the
pipeline yet" record -- see pipeline/runner.py. It carries no foreign key
onto messages or accounts, consistent with every other MailVerdict-owned
table (see database/models.py's module docstring).

pipeline_revisions is append-only: the current pipeline definition is
max(revision), never a row updated in place, so "what did the pipeline
look like before this edit" is always answerable by reading history rather
than by having kept a separate audit log.

pipeline_folder_state is MailVerdict's own watermark, one row per folder,
written when that folder's first full sync completes. It exists because
folders.initial_sync_done is a boolean with no timestamp, and a
reconciliation pass needs the timestamp to tell "arrived while
disconnected" from "historical" -- see pipeline/enqueue.py.

The old rules/spam settings are migrated by build_migrated_definition()
into the first pipeline_revisions row: the old rules engine, its executor,
and the old spam pipeline are deleted by this change (see CHANGELOG under
Breaking Changes), so an existing deployment's configuration has nowhere
else to live.

Revision ID: 0006_pipeline
Revises: 0005_message_embeddings
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_pipeline"
down_revision = "0005_message_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the pipeline tables and migrate settings.rules/spam.* into
    the first pipeline revision."""
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("msg_key", sa.Text, nullable=False),
        sa.Column("message_id", sa.Uuid, nullable=True),
        sa.Column("sweep_id", sa.Uuid, nullable=True),
        sa.Column("dedup_key", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("apply", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("skip_reason", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="100"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_by", sa.Text, nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_rev", sa.Integer, nullable=True),
        sa.Column("halted_at_stage", sa.Text, nullable=True),
        sa.Column("failed_stage", sa.Text, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("trace", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("model_calls", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'done', 'skipped', 'failed', 'cancelled')",
            name="ck_pipeline_run_status",
        ),
        sa.CheckConstraint("origin IN ('live', 'historical')", name="ck_pipeline_run_origin"),
        sa.UniqueConstraint("account_id", "msg_key", "dedup_key", name="uq_pipeline_run"),
    )
    op.create_index(
        "idx_pipeline_run_claim", "pipeline_runs", ["priority", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_pipeline_run_lease", "pipeline_runs", ["lease_expires_at"],
        postgresql_where=sa.text("status = 'claimed'"),
    )
    op.create_index(
        "idx_pipeline_run_failed", "pipeline_runs", ["account_id", "finished_at"],
        postgresql_where=sa.text("status = 'failed'"),
    )
    op.create_index(
        "idx_pipeline_run_sweep", "pipeline_runs", ["sweep_id"],
        postgresql_where=sa.text("sweep_id IS NOT NULL"),
    )

    op.create_table(
        "pipeline_revisions",
        sa.Column("revision", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "pipeline_folder_state",
        sa.Column("folder_id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("backfill_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    _migrate_settings_to_first_revision()


def _migrate_settings_to_first_revision() -> None:
    """Read settings.rules and settings.spam, build the first pipeline
    revision from them, and append it -- run in Python so the exact same
    migration function (build_migrated_definition) is used here and is
    unit-tested on its own, rather than a parallel SQL-only translation
    that could silently drift from it."""
    from mail_verdict.pipeline.revisions import build_migrated_definition

    bind = op.get_bind()

    settings = sa.table(
        "settings", sa.column("category", sa.String), sa.column("data", postgresql.JSONB),
    )
    rows = bind.execute(
        sa.select(settings.c.category, settings.c.data).where(
            settings.c.category.in_(["rules", "spam"])
        )
    ).all()
    by_category = {row.category: row.data for row in rows}

    raw_rules = (by_category.get("rules") or {}).get("rules", [])
    spam_settings = by_category.get("spam") or {}

    document = build_migrated_definition(raw_rules=raw_rules, spam_settings=spam_settings)

    revisions = sa.table(
        "pipeline_revisions",
        sa.column("document", postgresql.JSONB), sa.column("note", sa.Text),
    )
    bind.execute(
        sa.insert(revisions).values(
            document=document, note="Migrated from settings.rules and settings.spam",
        )
    )


def downgrade() -> None:
    """Drop the pipeline tables. settings.rules/spam are untouched by
    upgrade() and need no restoration."""
    op.drop_table("pipeline_folder_state")
    op.drop_table("pipeline_revisions")
    op.drop_index("idx_pipeline_run_sweep", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_run_failed", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_run_lease", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_run_claim", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
