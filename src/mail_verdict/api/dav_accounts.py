"""
DAV account API endpoints -- CalDAV/CardDAV servers.

GET    /api/dav-accounts               -- list, with each account's collections
GET    /api/dav-accounts/:id           -- one account
POST   /api/dav-accounts               -- add a server
PATCH  /api/dav-accounts/:id           -- update name/password/is_active
DELETE /api/dav-accounts/:id           -- remove the account and everything under it
POST   /api/dav-accounts/:id/sync      -- trigger an immediate sync

Requires PostIMAP >= 1.6.0 -- see postimap/contract.py's MIN_DAV_SERVICE_VERSION.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from mail_verdict.api.schemas import (
    DavAccountCreateRequest,
    DavAccountResponse,
    DavAccountUpdateRequest,
    DavCollectionSummary,
)
from mail_verdict.calendar.repository import DavAccountRepository
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import DavAccount, DavCollection
from mail_verdict.postimap.actions import (
    create_dav_account,
    delete_dav_account,
    force_reconnect_dav_account,
    update_dav_account,
)
from mail_verdict.postimap.commands import request_sync_now
from mail_verdict.postimap.contract import read_postimap_info, supports_dav

router = APIRouter(prefix="/dav-accounts", tags=["dav-accounts"])

_UNSUPPORTED_DETAIL = (
    "Calendars and contacts require PostIMAP service_version >= 1.6.0; "
    "the running instance reports {version}."
)


async def _require_support() -> None:
    db = get_db_connection()
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_dav(info):
        raise HTTPException(
            status_code=501,
            detail=_UNSUPPORTED_DETAIL.format(version=info.service_version if info else "unknown"),
        )


def _to_response(account: DavAccount, collections: list[DavCollection]) -> DavAccountResponse:
    return DavAccountResponse(
        id=account.id,
        name=account.name,
        discovery_url=account.url,
        username=account.username,
        is_active=account.is_active,
        state=account.state,  # type: ignore[arg-type]
        state_error=account.state_error,
        last_polled_at=account.last_polled_at,
        collections=[
            DavCollectionSummary(
                id=c.id, kind=c.kind, display_name=c.display_name,  # type: ignore[arg-type]
                sync_tier=c.sync_tier, initial_sync_done=c.initial_sync_done,
                total_count=c.total_count, backfill_total=c.backfill_total,
                last_synced_at=c.last_synced_at,
            )
            for c in collections
        ],
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get("", response_model=list[DavAccountResponse])
async def list_dav_accounts() -> list[DavAccountResponse]:
    """List every DAV account with its collections."""
    await _require_support()
    repo = DavAccountRepository(get_db_connection())
    accounts = await repo.list_all()
    responses = []
    for account in accounts:
        collections = await repo.list_collections(account.id)
        responses.append(_to_response(account, collections))
    return responses


@router.get("/{dav_account_id}", response_model=DavAccountResponse)
async def get_dav_account(dav_account_id: uuid.UUID) -> DavAccountResponse:
    """Get one DAV account with its collections."""
    await _require_support()
    repo = DavAccountRepository(get_db_connection())
    account = await repo.get_by_id(dav_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="DAV account not found")
    collections = await repo.list_collections(dav_account_id)
    return _to_response(account, collections)


@router.post("", response_model=DavAccountResponse, status_code=201)
async def create_dav_account_endpoint(request: DavAccountCreateRequest) -> DavAccountResponse:
    """Add a CalDAV/CardDAV server. PostIMAP discovers the principal and
    the two homes, then backfills every collection it finds."""
    await _require_support()
    db = get_db_connection()
    async with db.session() as session:
        account = await create_dav_account(
            session, name=request.name, url=request.discovery_url,
            username=request.username, password=request.password,
        )
        return _to_response(account, [])


@router.patch("/{dav_account_id}", response_model=DavAccountResponse)
async def update_dav_account_endpoint(
    dav_account_id: uuid.UUID, request: DavAccountUpdateRequest,
) -> DavAccountResponse:
    """Update name, password or is_active."""
    await _require_support()
    values = request.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")

    db = get_db_connection()
    repo = DavAccountRepository(db)
    existing = await repo.get_by_id(dav_account_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="DAV account not found")
    credentials_changed = "password" in values
    was_active = existing.is_active

    async with db.session() as session:
        await update_dav_account(session, dav_account_id, **values)

    if credentials_changed and was_active:
        # A credential rewritten on an already-running account is not
        # re-encrypted or used to reconnect until that account restarts --
        # the same trap api/accounts.py's update_account() documents for
        # mail. Without this, a corrected Nextcloud app password shows no
        # error and nothing changes, reading as the app ignoring the user.
        await force_reconnect_dav_account(db, dav_account_id)

    account = await repo.get_by_id(dav_account_id)
    assert account is not None
    collections = await repo.list_collections(dav_account_id)
    return _to_response(account, collections)


@router.delete("/{dav_account_id}", status_code=204)
async def delete_dav_account_endpoint(dav_account_id: uuid.UUID) -> None:
    """Permanently remove a DAV account and everything mirrored under it.
    Nothing on the server itself is touched."""
    await _require_support()
    db = get_db_connection()
    repo = DavAccountRepository(db)
    if await repo.get_by_id(dav_account_id) is None:
        raise HTTPException(status_code=404, detail="DAV account not found")
    async with db.session() as session:
        await delete_dav_account(session, dav_account_id)


@router.post("/{dav_account_id}/sync")
async def trigger_dav_sync(dav_account_id: uuid.UUID) -> dict[str, str]:
    """Wake this account's sync early, via the same postimap_commands
    channel mail accounts use -- both orchestrators listen, and the one
    owning the id acts."""
    await _require_support()
    repo = DavAccountRepository(get_db_connection())
    if await repo.get_by_id(dav_account_id) is None:
        raise HTTPException(status_code=404, detail="DAV account not found")
    await request_sync_now(get_db_connection(), dav_account_id)
    return {"status": "sync_requested", "dav_account_id": str(dav_account_id)}
