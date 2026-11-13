"""
Account display order API endpoint.

GET /api/account-order -- the caller's account display order
PUT /api/account-order -- save the caller's account display order

A per-user preference: the order is the `mail.account_order` user setting,
stored with the rest of the caller's own values (settings/service.py), and
only ever names accounts the caller owns. There is no PostIMAP trigger to
announce it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from mail_verdict.api.deps import current_scope
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.schemas import AccountOrderResponse, AccountOrderUpdate
from mail_verdict.auth.scope import Scope
from mail_verdict.settings import get_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account-order"])


def narrow_account_order(scope: Scope, raw: Any) -> list[str]:
    """
    The account ids of `raw` the caller owns, in order, as strings.

    Anything that is not a list, an entry that is not an id, an account that
    is not the caller's (gone, or someone else's) and a repeated id are
    dropped.

    Args:
        scope: The caller
        raw: A stored or client-sent order
    """
    if not isinstance(raw, list):
        return []
    order: list[str] = []
    for item in raw:
        try:
            account_id = uuid.UUID(str(item))
        except ValueError:
            continue
        if scope.owns_account(account_id) and str(account_id) not in order:
            order.append(str(account_id))
    return order


@router.get("/account-order", response_model=AccountOrderResponse)
async def get_account_order(scope: Scope = Depends(current_scope)) -> AccountOrderResponse:
    """Get the caller's account display order."""
    stored = get_settings_service().get_for_user("mail", scope.user_id)["account_order"]
    return AccountOrderResponse(
        order=[uuid.UUID(item) for item in narrow_account_order(scope, stored)],
    )


@router.put("/account-order", response_model=AccountOrderResponse)
async def set_account_order(
    request: AccountOrderUpdate, scope: Scope = Depends(current_scope),
) -> AccountOrderResponse:
    """Save the caller's account display order; accounts that are not theirs
    are left out."""
    order = narrow_account_order(scope, [str(account_id) for account_id in request.order])
    await get_settings_service().update_for_user(
        "mail", scope.require_user(), {"account_order": order}, is_admin=scope.is_admin,
    )

    # Reuses account.changed since the client already invalidates the
    # account list cache on it; the order is the caller's, so only their
    # streams hear it.
    event_ring = get_event_ring()
    if event_ring is not None:
        await event_ring.add(scope.require_user(), "account.changed", {})

    return AccountOrderResponse(order=[uuid.UUID(item) for item in order])
