"""Per-task AI providers: every task names its own provider and model.

Before, one provider (settings.ai.provider and base_url) served the spam
verdicts, the orders tasks and the rule assistant, and orders had a single
model, reasoning effort and token budget for both its calls. Now each task
carries its own set:

- orders: `model`, `reasoning_effort` and `max_tokens` become the
  `decide_*` and `write_*` groups, each also taking `provider` and
  `base_url` from the ai category it used to read them from. A deployment
  with a stored orders blob keeps behaving as it did: both tasks start on
  the values the one shared set had.
- assistant: a new category, seeded with whichever of provider, base_url,
  model and reasoning_effort the stored ai category holds.

The alert kind `provider_unavailable` is admitted by ck_alerts_kind: an AI
provider suspended for long raises one (alerts/provider_health.py).

Nothing is written for a setting that was never stored, so a fresh install
reads the code defaults. A stored value is copied, never reinterpreted.
Idempotent: a blob already in the new shape is left as it is.

Revision ID: 0048_task_providers
Revises: 0047_orders_wallet
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0048_task_providers"
down_revision: str | None = "0047_orders_wallet"
branch_labels: str | None = None
depends_on: str | None = None

_TASKS = ("decide", "write")
_OLD_ORDERS_KEYS = ("model", "reasoning_effort", "max_tokens")
# What the old shared keys read as when nothing was stored for them.
_OLD_ORDERS_DEFAULTS: dict[str, Any] = {"model": "", "reasoning_effort": "none", "max_tokens": 4000}
_AI_KEYS_COPIED_TO_ASSISTANT = ("provider", "base_url", "model", "reasoning_effort")

_PREVIOUS_ALERT_KINDS = (
    "'mail', 'reminder', 'outbox_stalled', 'glacier_conflict', 'webhook_failed'"
)
_ALERT_KINDS = f"{_PREVIOUS_ALERT_KINDS}, 'provider_unavailable'"

_settings = sa.table(
    "settings", sa.column("category", sa.String), sa.column("data", postgresql.JSONB),
)


def _read(category: str) -> dict[str, Any] | None:
    row = op.get_bind().execute(
        sa.select(_settings.c.data).where(_settings.c.category == category)
    ).first()
    return None if row is None else dict(row.data or {})


def _write(category: str, data: dict[str, Any]) -> None:
    insert = postgresql.insert(_settings).values(category=category, data=data)
    op.get_bind().execute(
        insert.on_conflict_do_update(index_elements=["category"], set_={"data": data})
    )


def upgrade() -> None:
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", f"kind IN ({_ALERT_KINDS})")

    ai = _read("ai") or {}
    orders = _read("orders")

    provider_keys = {k: ai[k] for k in ("provider", "base_url") if k in ai}
    shared_stored = orders is not None and any(k in orders for k in _OLD_ORDERS_KEYS)

    if orders is not None or provider_keys:
        migrated = {k: v for k, v in (orders or {}).items() if k not in _OLD_ORDERS_KEYS}
        for task in _TASKS:
            for key, value in provider_keys.items():
                migrated.setdefault(f"{task}_{key}", value)
            if shared_stored:
                for key in _OLD_ORDERS_KEYS:
                    old = (orders or {}).get(key, _OLD_ORDERS_DEFAULTS[key])
                    migrated.setdefault(f"{task}_{key}", old)
        _write("orders", migrated)

    if _read("assistant") is None:
        seed = {k: ai[k] for k in _AI_KEYS_COPIED_TO_ASSISTANT if k in ai}
        if seed:
            _write("assistant", seed)


def downgrade() -> None:
    """Fold the decide task's values back into the single shared set and
    drop the assistant category. Lossy when the two orders tasks differ:
    the write task's own model, effort and budget are discarded. Every
    provider_unavailable alert goes with its kind."""
    op.execute("DELETE FROM alerts WHERE kind = 'provider_unavailable'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", f"kind IN ({_PREVIOUS_ALERT_KINDS})")

    orders = _read("orders")
    if orders is not None:
        folded = {
            k: v for k, v in orders.items()
            if not any(k.startswith(f"{task}_") for task in _TASKS)
        }
        for key in _OLD_ORDERS_KEYS:
            if f"decide_{key}" in orders:
                folded[key] = orders[f"decide_{key}"]
        _write("orders", folded)
    op.get_bind().execute(sa.delete(_settings).where(_settings.c.category == "assistant"))
