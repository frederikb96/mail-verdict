"""
Account API endpoints.

GET /api/accounts — list all accounts
POST /api/accounts — create account (encrypts passwords)
GET /api/accounts/:id — account detail
PUT /api/accounts/:id — update account
DELETE /api/accounts/:id — delete account
POST /api/accounts/:id/test-connection — test IMAP/SMTP connectivity
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select, update

from mail_verdict.api.schemas import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    FolderResponse,
)
from mail_verdict.core.encryption import encrypt
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts() -> list[AccountResponse]:
    """List all configured accounts."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).order_by(Account.name))
        accounts = list(result.scalars().all())
    return [AccountResponse.model_validate(a) for a in accounts]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(request: AccountCreateRequest) -> AccountResponse:
    """Create a new IMAP account with encrypted passwords."""
    db = get_db_connection()
    account = Account(
        name=request.name,
        imap_host=request.imap_host,
        imap_port=request.imap_port,
        imap_user=request.imap_user,
        imap_password=encrypt(request.imap_password) if request.imap_password else None,
        smtp_host=request.smtp_host,
        smtp_port=request.smtp_port,
        smtp_user=request.smtp_user,
        smtp_password=encrypt(request.smtp_password) if request.smtp_password else None,
        is_active=request.is_active,
        sync_lookback_days=request.sync_lookback_days,
        embedding_lookback_days=request.embedding_lookback_days,
        spam_enabled=request.spam_enabled,
    )
    async with db.session() as session:
        session.add(account)
        await session.flush()
        await session.refresh(account)
        return AccountResponse.model_validate(account)


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: uuid.UUID) -> AccountResponse:
    """Get account detail by ID."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountResponse.model_validate(account)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    request: AccountUpdateRequest,
) -> AccountResponse:
    """Update an existing account. Passwords are re-encrypted if provided."""
    db = get_db_connection()
    update_values = request.model_dump(exclude_unset=True)
    if not update_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "imap_password" in update_values and update_values["imap_password"] is not None:
        update_values["imap_password"] = encrypt(update_values["imap_password"])
    if "smtp_password" in update_values and update_values["smtp_password"] is not None:
        update_values["smtp_password"] = encrypt(update_values["smtp_password"])

    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(
            update(Account).where(Account.id == account_id).values(**update_values)
        )
        await session.refresh(account)
        return AccountResponse.model_validate(account)


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
    """Test IMAP/SMTP connectivity for an account."""
    from mail_verdict.core.encryption import decrypt

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    results: dict[str, str] = {}

    try:
        import asyncio

        from imap_tools import BaseMailBox, MailBox, MailBoxStartTls, MailBoxUnencrypted

        password = decrypt(account.imap_password) if account.imap_password else ""
        use_ssl = account.imap_port in (993, 995)
        port = account.imap_port

        def _sync_test_imap() -> str:
            try:
                mb: BaseMailBox
                if use_ssl:
                    mb = MailBox(account.imap_host, port)
                elif port == 143:
                    mb = MailBoxStartTls(account.imap_host, port)
                else:
                    mb = MailBoxUnencrypted(account.imap_host, port)
                mb.login(account.imap_user, password, initial_folder=None)
                mb.logout()
                return "ok"
            except Exception as exc:
                return f"error: {exc}"

        results["imap"] = await asyncio.to_thread(_sync_test_imap)
    except Exception as e:
        results["imap"] = f"error: {e}"

    if account.smtp_host:
        try:
            import aiosmtplib

            password = decrypt(account.smtp_password) if account.smtp_password else ""
            port = account.smtp_port or 465
            use_tls = port == 465
            start_tls = port == 587
            smtp = aiosmtplib.SMTP(
                hostname=account.smtp_host,
                port=port,
                use_tls=use_tls,
                start_tls=start_tls,
            )
            await smtp.connect()
            await smtp.login(account.smtp_user or account.imap_user, password)
            results["smtp"] = "ok"
            await smtp.quit()
        except Exception as e:
            results["smtp"] = f"error: {e}"

    return results


@router.post("/{account_id}/sync")
async def trigger_sync(account_id: uuid.UUID) -> dict[str, str]:
    """Trigger an immediate sync cycle for this account."""
    from mail_verdict.server import get_sync_engine
    from mail_verdict.settings.service import get_settings_service

    # Check global sync enabled
    svc = get_settings_service()
    if svc:
        sync_cfg = svc.get("sync")
        if not sync_cfg.get("enabled", True):
            raise HTTPException(status_code=409, detail="Global sync is disabled")

    engine = get_sync_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Sync engine not initialized")

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.is_active:
        raise HTTPException(status_code=409, detail="Account is disabled")

    # Check if account already has sync infrastructure
    account_name = account.name
    if account_name not in engine._accounts:
        # Dynamically add this account to the sync engine
        await engine.add_account(account)
        return {"status": "sync_started", "message": f"Account '{account_name}' sync started"}

    # Trigger immediate poll on existing account
    manager = engine._accounts[account_name].manager
    await manager.trigger_now()
    return {"status": "sync_triggered", "message": f"Immediate sync triggered for '{account_name}'"}


@router.delete("/{account_id}/sync")
async def cancel_sync(account_id: uuid.UUID) -> dict[str, str]:
    """Cancel sync and disable the account."""
    from mail_verdict.server import get_sync_engine

    engine = get_sync_engine()
    db = get_db_connection()

    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # Set is_active = False in DB
    async with db.session() as session:
        await session.execute(
            update(Account).where(Account.id == account_id).values(is_active=False)
        )

    # Stop sync if running
    if engine and account.name in engine._accounts:
        await engine.remove_account(account.name)

    return {"status": "cancelled", "message": "Sync cancelled and account disabled"}


@router.get("/{account_id}/sync/status")
async def sync_status(account_id: uuid.UUID) -> dict[str, Any]:
    """Get sync status for an account."""
    from mail_verdict.server import get_sync_engine

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    engine = get_sync_engine()
    is_syncing = False
    if engine and account.name in engine._accounts:
        is_syncing = True

    return {
        "account_id": str(account_id),
        "state": account.state.value,
        "is_active": account.is_active,
        "is_syncing": is_syncing,
    }


@router.get("/{account_id}/folders", response_model=list[FolderResponse])
async def list_folders(account_id: uuid.UUID) -> list[FolderResponse]:
    """List all folders for an account with message counts."""
    from sqlalchemy import case
    from sqlalchemy import func as sa_func

    from mail_verdict.database.models import Mail

    db = get_db_connection()
    async with db.session() as session:
        # Query folders with aggregated message counts
        stmt = (
            select(
                Folder,
                sa_func.count(Mail.id).label("total_count"),
                sa_func.count(
                    case((Mail.is_read.is_(False), Mail.id))
                ).label("unread_count"),
            )
            .outerjoin(
                Mail,
                (Mail.folder_id == Folder.id) & Mail.is_deleted.is_(False),
            )
            .where(Folder.account_id == account_id)
            .group_by(Folder.id)
            .order_by(Folder.imap_name)
        )
        result = await session.execute(stmt)
        rows = list(result.all())

    return [
        FolderResponse(
            id=f.id,
            account_id=f.account_id,
            imap_name=f.imap_name,
            display_name=f.display_name,
            special_use=f.special_use.value if f.special_use else None,
            subscribed=f.subscribed,
            is_visible=f.is_visible,
            last_synced_at=f.last_synced_at,
            total_count=total,
            unread_count=unread,
        )
        for f, total, unread in rows
    ]


@router.get("/{account_id}/folder-mapping")
async def get_folder_mapping(account_id: uuid.UUID) -> dict[str, str | None]:
    """Get the folder mapping for an account (auto-detect if not set)."""
    from mail_verdict.sync.folder_mapping import auto_detect_mapping

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if account.folder_mapping:
        return account.folder_mapping

    # Auto-detect from synced folders
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(Folder.account_id == account_id)
        )
        folder_rows = list(result.scalars().all())  # type: ignore[assignment]

    folder_dicts: list[dict[str, str | None]] = [
        {
            "imap_name": f.imap_name,  # type: ignore[attr-defined]
            "special_use": f.special_use.value if f.special_use else None,  # type: ignore[attr-defined]
        }
        for f in folder_rows
    ]
    return auto_detect_mapping(folder_dicts)


@router.put("/{account_id}/folder-mapping")
async def update_folder_mapping(
    account_id: uuid.UUID,
    mapping: dict[str, str | None],
) -> dict[str, str | None]:
    """Save custom folder mapping for an account."""
    from sqlalchemy import update as sql_update

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(
            sql_update(Account).where(Account.id == account_id).values(folder_mapping=mapping)
        )
    return mapping
