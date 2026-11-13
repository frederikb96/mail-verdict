"""
Telling a user's open clients that one of their orders changed.

An order belongs to one user, so its `order.updated` event goes on that
user's ring key (auth/scope.py's ring_keys) and reaches no one else.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing

# (user_id, order_id, change) -- what orders/ownership.py's reassign_account
# returns for the caller to announce once its transaction has committed.
OrderChange = tuple[uuid.UUID, uuid.UUID, str]


async def announce_changes(event_ring: EventRing | None, changes: list[OrderChange]) -> None:
    """Tell each user's open clients their order changed: (user_id, order_id,
    change) as `reassign_account` returns them."""
    if event_ring is None:
        return
    for user_id, order_id, change in changes:
        await announce(event_ring, user_id, order_id, change)


async def announce(
    event_ring: EventRing | None, user_id: uuid.UUID | None, order_id: uuid.UUID, change: str,
) -> None:
    """Push order.updated to the user the order belongs to (no one for an
    order nobody owns)."""
    if event_ring is None or user_id is None:
        return
    await event_ring.add(
        user_id, "order.updated", {"order_id": str(order_id), "change": change},
    )
