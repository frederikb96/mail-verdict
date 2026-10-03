"""Heal a pipeline_revisions.document written as a jsonb string instead of
a jsonb object.

0038_orders's own data-migration step handed an already-serialized JSON
string to a JSONB-typed Core column, which serializes whatever it is
given -- so the string was encoded a second time, producing a jsonb
*string* holding the encoded document as text (jsonb_typeof = 'string')
rather than a jsonb *object*. Every reader of the current pipeline
revision expects an object and calls .get() on it, so a database that
already ran that migration is left with a current revision nothing can
read: the pipeline API, its health route, and the classification runner
(which reads the current revision at the top of every run) all fail.

This repairs any row shaped that way, in place, by unwrapping the stored
text and re-parsing it as JSON -- the content is byte-for-byte the same
document, only its jsonb type corrected from string to object; no stage
is added, removed or reordered. Scoped by jsonb_typeof(document) =
'string', so it is a no-op on a database that never produced one (a
fresh install, using the fixed migration, or one already healed) --
idempotent.

Revision ID: 0039_heal_pipeline_document
Revises: 0038_orders
"""

from __future__ import annotations

from alembic import op

revision: str = "0039_heal_pipeline_document"
down_revision: str | None = "0038_orders"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Unwrap any pipeline_revisions.document stored as a jsonb string
    back into the jsonb object it always was."""
    op.execute(
        "UPDATE pipeline_revisions "
        "SET document = (document #>> '{}')::jsonb "
        "WHERE jsonb_typeof(document) = 'string'"
    )


def downgrade() -> None:
    """No-op: a jsonb object holding the document is the correct shape,
    the same double-encoding bug this undoes is not something to restore."""
