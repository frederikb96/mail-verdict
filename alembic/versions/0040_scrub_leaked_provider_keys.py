"""Scrub any provider-key-shaped field out of settings.data.

The settings API only ever stripped a raw provider key (and its computed
"_configured"/"_hint" status) from the "ai" category's own JSONB blob --
every other category's PUT and bulk-import wrote the request body
through unfiltered. A key sent to any other category (the "semantic"
category's shared "custom" key among them, since one credential serves
whichever category has its own provider set to "custom") was merged into
that category's stored blob in the clear and read back that way on every
GET from then on.

This does not rotate or invalidate anything -- a key already exposed this
way needs rotating with its provider regardless of what runs here. It
only removes the plaintext copy sitting in settings.data so a future read
of an already-poisoned row can no longer return it, matching the API's
own read-time stripping added alongside this migration. A no-op on any
database that never had a key-shaped field written into a category's
blob -- idempotent, and safe to run on a fresh install.

Revision ID: 0040_scrub_leaked_provider_keys
Revises: 0039_heal_pipeline_document
"""

from __future__ import annotations

from alembic import op

revision: str = "0040_scrub_leaked_provider_keys"
down_revision: str | None = "0039_heal_pipeline_document"
branch_labels: str | None = None
depends_on: str | None = None

# Mirrors settings_api.py's _CREDENTIAL_FIELD_PATTERN: a raw provider key
# (e.g. "openai_api_key") or its computed status suffix, whichever
# category's blob it was written into.
_CREDENTIAL_FIELD_REGEX = "^\\w+_api_key(_configured|_hint)?$"


def upgrade() -> None:
    """Drop every credential-shaped key from every settings row's JSONB
    blob, keeping everything else in the row unchanged."""
    op.execute(
        "UPDATE settings "
        "SET data = COALESCE("
        "  (SELECT jsonb_object_agg(kv.key, kv.value) "
        "   FROM jsonb_each(data) AS kv(key, value) "
        f"  WHERE kv.key !~ '{_CREDENTIAL_FIELD_REGEX}'), "
        "  '{}'::jsonb"
        ") "
        "WHERE EXISTS ("
        "  SELECT 1 FROM jsonb_each(data) AS kv(key, value) "
        f"  WHERE kv.key ~ '{_CREDENTIAL_FIELD_REGEX}'"
        ")"
    )


def downgrade() -> None:
    """No-op: a plaintext provider key never belonged in settings.data,
    the leak this undoes is not something to restore."""
