"""
Account display order API endpoint.

GET /api/account-order -- stored account display order
PUT /api/account-order -- save account display order

A single, instance-wide preference rather than a per-account one, so it is
stored the same way the unified folder order is: one row in the settings
table keyed by its own category, with no PostIMAP trigger to announce it
(see unified.py's UNIFIED_VIEW_CATEGORY for the identical shape).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter
from sqlalchemy import select, update

from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.api.schemas import AccountOrderResponse, AccountOrderUpdate
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Setting

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account-order"])

ACCOUNT_ORDER_CATEGORY = "account_order"


async def _get_account_order() -> list[uuid.UUID]:
    """Read account display order from the settings table."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Setting).where(Setting.category == ACCOUNT_ORDER_CATEGORY)
        )
        setting = result.scalar_one_or_none()

    if not setting or not isinstance(setting.data, dict):
        return []

    order: list[uuid.UUID] = []
    for item in setting.data.get("order", []):
        try:
            order.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return order


@router.get("/account-order", response_model=AccountOrderResponse)
async def get_account_order() -> AccountOrderResponse:
    """Get the stored account display order."""
    return AccountOrderResponse(order=await _get_account_order())


@router.put("/account-order", response_model=AccountOrderResponse)
async def set_account_order(request: AccountOrderUpdate) -> AccountOrderResponse:
    """Save the account display order."""
    order_strs = [str(account_id) for account_id in request.order]
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Setting).where(Setting.category == ACCOUNT_ORDER_CATEGORY)
        )
        existing = result.scalar_one_or_none()

        if existing:
            data = dict(existing.data) if existing.data else {}
            data["order"] = order_strs
            await session.execute(
                update(Setting)
                .where(Setting.category == ACCOUNT_ORDER_CATEGORY)
                .values(data=data)
            )
        else:
            session.add(
                Setting(category=ACCOUNT_ORDER_CATEGORY, data={"order": order_strs})
            )

    # This Setting row is not account-scoped, so there is no single
    # account_id to key the event on -- broadcast to every account's ring
    # instead (see broadcast_event). Reuses account.changed since the
    # client already invalidates the account list cache on it.
    event_ring = get_event_ring()
    if event_ring is not None:
        await broadcast_event(db, event_ring, "account.changed", {})

    return AccountOrderResponse(order=request.order)
