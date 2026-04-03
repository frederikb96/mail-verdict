"""
Account API endpoints.

GET /api/accounts — list all accounts
POST /api/accounts — create account (encrypts passwords)
GET /api/accounts/:id — account detail
PUT /api/accounts/:id — update account
DELETE /api/accounts/:id — delete account
POST /api/accounts/:id/test-connection — test IMAP/SMTP connectivity
GET /api/accounts/:id/folders — folder listing with counts
GET /api/accounts/:id/folder-mapping — get/auto-detect folder mapping
PUT /api/accounts/:id/folder-mapping — save folder mapping

PostIMAP integration: accounts table is PostIMAP-owned.
AccountPrefs stores MailVerdict-specific preferences.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import case, delete, select, update
from sqlalchemy import func as sa_func

from mail_verdict.api.deps import get_account_prefs_repo
from mail_verdict.api.schemas import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    FolderResponse,
)
from mail_verdict.core.encryption import encrypt
from mail_verdict.core.jsonb import parse_jsonb
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, AccountPrefs, Folder, FolderPrefs, Message

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
        capabilities=parse_jsonb(account.capabilities),
        created_at=account.created_at,
        updated_at=account.updated_at,
        emoji=prefs.emoji if prefs else None,
        spam_enabled=prefs.spam_enabled if prefs else False,
        embedding_lookback_days=prefs.embedding_lookback_days if prefs else 30,
        folder_mapping=prefs.folder_mapping if prefs else None,
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
    """Create a new IMAP account with encrypted passwords."""
    db = get_db_connection()
    account = Account(
        name=request.name,
        imap_host=request.imap_host,
        imap_port=request.imap_port,
        imap_user=request.imap_user,
        imap_password=encrypt(request.imap_password) if request.imap_password else b"",
        smtp_host=request.smtp_host,
        smtp_port=request.smtp_port,
        smtp_user=request.smtp_user,
        smtp_password=encrypt(request.smtp_password) if request.smtp_password else None,
        is_active=request.is_active,
    )
    async with db.session() as session:
        session.add(account)
        await session.flush()
        await session.refresh(account)

        # Create AccountPrefs record
        prefs = AccountPrefs(
            account_id=account.id,
            emoji=request.emoji,
            spam_enabled=request.spam_enabled,
            embedding_lookback_days=request.embedding_lookback_days,
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


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    request: AccountUpdateRequest,
) -> AccountResponse:
    """Update an existing account. Passwords are re-encrypted if provided."""
    db = get_db_connection()
    all_values = request.model_dump(exclude_unset=True)
    if not all_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Separate Account fields from AccountPrefs fields
    prefs_fields = {"emoji", "embedding_lookback_days", "spam_enabled"}
    account_values = {k: v for k, v in all_values.items() if k not in prefs_fields}
    prefs_values = {k: v for k, v in all_values.items() if k in prefs_fields}

    if "imap_password" in account_values and account_values["imap_password"] is not None:
        account_values["imap_password"] = encrypt(account_values["imap_password"])
    if "smtp_password" in account_values and account_values["smtp_password"] is not None:
        account_values["smtp_password"] = encrypt(account_values["smtp_password"])

    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        if account_values:
            await session.execute(
                update(Account).where(Account.id == account_id).values(**account_values)
            )

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
    """Delete an account and all its data (cascading)."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(delete(Account).where(Account.id == account_id))


@router.post("/{account_id}/test-connection")
async def test_connection(account_id: uuid.UUID) -> dict[str, str]:
    """Test connectivity — delegated to PostIMAP (check account state)."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return {
        "imap": "ok" if account.state == "active" else f"state: {account.state}",
        "state_error": account.state_error or "",
    }


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
                (Message.folder_id == Folder.id) & Message.deleted_at.is_(None),
            )
            .where(Folder.account_id == account_id)
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
            special_use=f.special_use,
            mailbox_id=f.mailbox_id,
            exists_count=f.exists_count,
            last_synced_at=f.last_synced_at,
            sync_error=f.sync_error,
            created_at=f.created_at,
            unified_name=fp.unified_name if fp else None,
            subscribed=fp.subscribed if fp else True,
            is_visible=fp.is_visible if fp else True,
            total_count=total,
            unread_count=unread,
        )
        for f, fp, total, unread in rows
    ]


@router.get("/{account_id}/folder-mapping")
async def get_folder_mapping(account_id: uuid.UUID) -> dict[str, str | None]:
    """
    Get the folder mapping for an account.

    Returns stored mapping from AccountPrefs, or auto-detects
    from Folder.special_use if not set.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # Check AccountPrefs for stored mapping
    prefs_repo = get_account_prefs_repo()
    prefs = await prefs_repo.get_by_account(account_id)
    if prefs and prefs.folder_mapping:
        return prefs.folder_mapping

    # Auto-detect from Folder.special_use
    from mail_verdict.database.repository import FolderRepository

    folder_repo = FolderRepository(db)
    all_folders = await folder_repo.get_by_account(account_id)

    mapping: dict[str, str | None] = {
        "inbox": None,
        "sent": None,
        "drafts": None,
        "trash": None,
        "junk": None,
        "archive": None,
    }
    for folder in all_folders:
        if folder.special_use and folder.special_use in mapping:
            mapping[folder.special_use] = folder.imap_name
    return mapping


@router.put("/{account_id}/folder-mapping")
async def update_folder_mapping(
    account_id: uuid.UUID,
    mapping: dict[str, str | None],
) -> dict[str, str | None]:
    """Save custom folder mapping for an account in AccountPrefs."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

    prefs_repo = get_account_prefs_repo()
    await prefs_repo.update(account_id, folder_mapping=mapping)
    return mapping
