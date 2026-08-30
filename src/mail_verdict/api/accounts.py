"""
Account API endpoints.

GET /api/accounts — list all accounts
POST /api/accounts — create account (writes credentials in contract format)
GET /api/accounts/:id — account detail
PATCH /api/accounts/:id — update account (bounces sync if credentials changed)
DELETE /api/accounts/:id — delete account (requires PostIMAP >= 1.0.1)
GET /api/accounts/:id/folders — folder listing with counts
GET /api/accounts/:id/sync-status — sync progress from PostIMAP sync_state
POST /api/accounts/:id/sync — trigger immediate sync via PG NOTIFY

PostIMAP integration: accounts table is PostIMAP-owned. AccountPrefs stores
MailVerdict-specific preferences. account.state/state_error is the
connectivity truth surface -- there is no separate test-connection probe,
and never any direct IMAP import.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import case, select
from sqlalchemy import func as sa_func

from mail_verdict.api.deps import get_account_prefs_repo
from mail_verdict.api.schemas import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    FolderResponse,
    SyncStatusResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Folder,
    FolderPrefs,
    Message,
    SyncState,
)
from mail_verdict.postimap.actions import create_account as postimap_create_account
from mail_verdict.postimap.actions import delete_account as postimap_delete_account
from mail_verdict.postimap.actions import force_reconnect
from mail_verdict.postimap.actions import update_account as postimap_update_account
from mail_verdict.postimap.commands import request_sync_now
from mail_verdict.postimap.contract import read_postimap_info, supports_account_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _build_account_response(
    account: Account,
    prefs: AccountPrefs | None = None,
) -> AccountResponse:
    """
    Build an AccountResponse combining Account + AccountPrefs fields.

    Args:
        account: PostIMAP Account model
        prefs: Optional MailVerdict AccountPrefs

    Returns:
        AccountResponse with merged fields
    """
    return AccountResponse(
        id=account.id,
        name=account.name,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        imap_user=account.imap_user,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        smtp_user=account.smtp_user,
        is_active=account.is_active,
        state=account.state,
        state_error=account.state_error,
        capabilities=account.capabilities,
        created_at=account.created_at,
        updated_at=account.updated_at,
        emoji=prefs.emoji if prefs else None,
        spam_enabled=prefs.spam_enabled if prefs else False,
        folder_order=prefs.folder_order if prefs else None,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts() -> list[AccountResponse]:
    """List all configured accounts with their preferences."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Account, AccountPrefs)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .order_by(Account.name)
        )
        rows = list(result.all())
    return [_build_account_response(acct, prefs) for acct, prefs in rows]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(request: AccountCreateRequest) -> AccountResponse:
    """Create a new IMAP account.

    Credentials are written in the contract's consumer format (plaintext,
    0x00-prefixed) via postimap/actions.py; PostIMAP re-encrypts them
    itself once it picks the account up.
    """
    db = get_db_connection()
    async with db.session() as session:
        account = await postimap_create_account(
            session,
            name=request.name,
            imap_host=request.imap_host,
            imap_port=request.imap_port,
            imap_user=request.imap_user,
            imap_password=request.imap_password or "",
            smtp_host=request.smtp_host,
            smtp_port=request.smtp_port,
            smtp_user=request.smtp_user,
            smtp_password=request.smtp_password,
            is_active=request.is_active,
        )

        prefs = AccountPrefs(
            account_id=account.id,
            emoji=request.emoji,
            spam_enabled=request.spam_enabled,
        )
        session.add(prefs)
        await session.flush()
        await session.refresh(prefs)

        return _build_account_response(account, prefs)


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: uuid.UUID) -> AccountResponse:
    """Get account detail by ID."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Account, AccountPrefs)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .where(Account.id == account_id)
        )
        row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account, prefs = row
    return _build_account_response(account, prefs)


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    request: AccountUpdateRequest,
) -> AccountResponse:
    """Update an existing account. Passwords are re-formatted if provided."""
    db = get_db_connection()
    all_values = request.model_dump(exclude_unset=True)
    if not all_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Separate Account fields from AccountPrefs fields
    prefs_fields = {"emoji", "spam_enabled"}
    account_values = {k: v for k, v in all_values.items() if k not in prefs_fields}
    prefs_values = {k: v for k, v in all_values.items() if k in prefs_fields}

    credentials_changed = "imap_password" in account_values or "smtp_password" in account_values

    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        was_active = account.is_active

        if account_values:
            await postimap_update_account(session, account_id, **account_values)

    if credentials_changed and was_active:
        # A credential rewritten on an already-running account is not
        # re-encrypted or used to reconnect until that account restarts --
        # without this, a corrected password shows no error and nothing
        # changes, which reads as the app ignoring the user.
        await force_reconnect(db, account_id)

    # Update prefs if any prefs fields were provided
    if prefs_values:
        prefs_repo = get_account_prefs_repo()
        await prefs_repo.update(account_id, **prefs_values)

    # Re-fetch to return updated state
    async with db.session() as session:
        result = await session.execute(
            select(Account, AccountPrefs)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .where(Account.id == account_id)
        )
        row = result.one()
        acct, prefs = row
        return _build_account_response(acct, prefs)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID) -> None:
    """
    Permanently delete an account and its entire mirrored mailbox.

    Requires PostIMAP service_version >= 1.0.1 (the DELETE grant on
    accounts). Against an older PostIMAP this reports the capability as
    unavailable rather than attempting a statement that fails on grants.
    Irreversible; touches nothing on the IMAP server itself.
    """
    db = get_db_connection()
    async with db.session() as session:
        info = await read_postimap_info(session)
        if info is None or not supports_account_delete(info):
            raise HTTPException(
                status_code=501,
                detail=(
                    "Account deletion requires PostIMAP service_version >= 1.0.1; "
                    f"the running instance reports "
                    f"{info.service_version if info else 'unknown'}. "
                    "Set is_active=false to pause syncing instead."
                ),
            )

        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await postimap_delete_account(session, account_id)


@router.get("/{account_id}/folders", response_model=list[FolderResponse])
async def list_folders(account_id: uuid.UUID) -> list[FolderResponse]:
    """List all folders for an account with message counts and prefs."""
    db = get_db_connection()
    async with db.session() as session:
        # Query folders with prefs and aggregated message counts
        stmt = (
            select(
                Folder,
                FolderPrefs,
                sa_func.count(Message.id).label("total_count"),
                sa_func.count(
                    case((Message.is_seen.is_(False), Message.id))
                ).label("unread_count"),
            )
            .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .outerjoin(
                Message,
                (Message.folder_id == Folder.id) & Message.expunged_at.is_(None),
            )
            .where(Folder.account_id == account_id, Folder.deleted_at.is_(None))
            .group_by(Folder.id, FolderPrefs.folder_id)
            .order_by(Folder.imap_name)
        )
        result = await session.execute(stmt)
        rows = list(result.all())

    return [
        FolderResponse(
            id=f.id,
            account_id=f.account_id,
            imap_name=f.imap_name,
            display_name=f.display_name or (fp.display_name if fp else None),
            special_use=(fp.special_use_override if fp else None) or f.special_use,
            mailbox_id=f.mailbox_id,
            initial_sync_done=f.initial_sync_done,
            backfill_total=f.backfill_total,
            idle_requested=f.idle_requested,
            idle_status=f.idle_status,
            last_synced_at=f.last_synced_at,
            sync_error=f.sync_error,
            created_at=f.created_at,
            unified_name=fp.unified_name if fp else None,
            is_visible=fp.is_visible if fp else True,
            total_count=total,
            unread_count=unread,
        )
        for f, fp, total, unread in rows
    ]


@router.get("/{account_id}/sync-status", response_model=SyncStatusResponse)
async def get_sync_status(account_id: uuid.UUID) -> SyncStatusResponse:
    """Get sync status for an account (reads PostIMAP's sync_state table).

    Combines the account lifecycle state from accounts.state with
    detailed sync progress from the sync_state table.

    Args:
        account_id: UUID of the account to query.

    Returns:
        SyncStatusResponse with combined account state and sync progress.

    Raises:
        HTTPException: 404 if account not found.
    """
    db = get_db_connection()
    async with db.session() as session:
        acct_result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = acct_result.scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        sync_result = await session.execute(
            select(SyncState).where(SyncState.account_id == account_id)
        )
        ss = sync_result.scalar_one_or_none()

        return SyncStatusResponse(
            account_id=account.id,
            state=account.state,
            state_error=account.state_error,
            last_full_sync=ss.last_full_sync if ss else None,
            last_incr_sync=ss.last_incr_sync if ss else None,
            sync_tier=ss.sync_tier if ss else None,
            folders_synced=ss.folders_synced if ss else 0,
            folders_total=ss.folders_total if ss else 0,
            messages_synced=ss.messages_synced if ss else 0,
            error_count=ss.error_count if ss else 0,
            last_error=ss.last_error if ss else None,
            updated_at=ss.updated_at if ss else None,
        )


@router.post("/{account_id}/sync")
async def trigger_sync(account_id: uuid.UUID) -> dict[str, str]:
    """Trigger an immediate sync for an account via PG NOTIFY to PostIMAP.

    Sends a JSON payload on the 'postimap_commands' PG NOTIFY channel,
    which PostIMAP listens on to initiate an out-of-band sync cycle.

    Args:
        account_id: UUID of the account to sync.

    Returns:
        Dict confirming the sync request was sent.

    Raises:
        HTTPException: 404 if account not found.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

    await request_sync_now(db, account_id)

    return {"status": "sync_requested", "account_id": str(account_id)}
