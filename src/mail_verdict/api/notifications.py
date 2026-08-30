"""
Notification centre API.

GET  /api/accounts/:id/notifications              -- list, newest first
GET  /api/accounts/:id/notifications/unacknowledged-count -- badge count
POST /api/accounts/:id/notifications/:notification_id/ack -- acknowledge one
POST /api/accounts/:id/notifications/ack-all       -- acknowledge every
  unacknowledged notification for the account

Built on PostIMAP's sync_notifications: a durable, acknowledgeable record
of a write that gave up permanently, including a send that never left.
Requires PostIMAP >= 1.3.0 -- see postimap/contract.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from mail_verdict.api.deps import get_sync_notification_repo
from mail_verdict.api.schemas import NotificationCountResponse, NotificationResponse
from mail_verdict.database.connection import get_db_connection
from mail_verdict.postimap.actions import acknowledge_all_notifications, acknowledge_notification
from mail_verdict.postimap.contract import read_postimap_info, supports_sync_notifications

router = APIRouter(prefix="/accounts/{account_id}/notifications", tags=["notifications"])

_UNSUPPORTED_DETAIL = (
    "The notification centre requires PostIMAP service_version >= 1.3.0; "
    "the running instance reports {version}."
)


async def _require_support() -> None:
    """Raise 501 unless the running PostIMAP has sync_notifications."""
    db = get_db_connection()
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_sync_notifications(info):
        raise HTTPException(
            status_code=501,
            detail=_UNSUPPORTED_DETAIL.format(version=info.service_version if info else "unknown"),
        )


def _to_response(row: object) -> NotificationResponse:
    return NotificationResponse.model_validate(row, from_attributes=True)


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    account_id: uuid.UUID,
    unacknowledged_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[NotificationResponse]:
    """List notifications for an account, newest first."""
    await _require_support()
    repo = get_sync_notification_repo()
    rows = await repo.list_for_account(
        account_id, unacknowledged_only=unacknowledged_only, limit=limit,
    )
    return [_to_response(row) for row in rows]


@router.get("/unacknowledged-count", response_model=NotificationCountResponse)
async def get_unacknowledged_count(account_id: uuid.UUID) -> NotificationCountResponse:
    """Unacknowledged notification count -- what a bell badge renders."""
    await _require_support()
    repo = get_sync_notification_repo()
    count = await repo.unacknowledged_count(account_id)
    return NotificationCountResponse(unacknowledged=count)


@router.post("/{notification_id}/ack", status_code=204)
async def acknowledge(account_id: uuid.UUID, notification_id: int) -> None:
    """Acknowledge one notification."""
    await _require_support()
    db = get_db_connection()
    async with db.session() as session:
        await acknowledge_notification(session, notification_id)


@router.post("/ack-all", status_code=204)
async def acknowledge_all(account_id: uuid.UUID) -> None:
    """Acknowledge every unacknowledged notification for the account."""
    await _require_support()
    db = get_db_connection()
    async with db.session() as session:
        await acknowledge_all_notifications(session, account_id)
