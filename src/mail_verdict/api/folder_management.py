"""
Folder management API endpoints.

Folder ordering, visibility, and IDLE configuration per account.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import case, select, update
from sqlalchemy import func as sa_func

from mail_verdict.api.schemas import (
    FolderOrderItem,
    FolderOrderResponse,
    FolderOrderUpdate,
    FolderVisibilityResponse,
    FolderVisibilityUpdate,
    IdleFolderItem,
    IdleFolderToggle,
    IdleFolderToggleResponse,
    IdleValidationRequest,
    IdleValidationResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, Mail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts/{account_id}", tags=["folder-management"])


async def _get_account_or_404(account_id: uuid.UUID) -> Account:
    """Fetch account by ID or raise 404."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


async def _get_folders_with_counts(
    account_id: uuid.UUID,
) -> list[tuple[Folder, int, int]]:
    """Get all folders for an account with unread/total counts."""
    db = get_db_connection()
    async with db.session() as session:
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
        )
        result = await session.execute(stmt)
        return list(result.all())  # type: ignore[arg-type]


# --- Folder Ordering + Visibility ---


@router.get("/folder-order", response_model=FolderOrderResponse)
async def get_folder_order(account_id: uuid.UUID) -> FolderOrderResponse:
    """Get ordered folder list with visibility and counts."""
    account = await _get_account_or_404(account_id)
    rows = await _get_folders_with_counts(account_id)

    # Build lookup by folder ID
    folder_map: dict[uuid.UUID, tuple[Folder, int, int]] = {
        f.id: (f, total, unread) for f, total, unread in rows
    }

    # Apply custom order if set, otherwise alphabetical
    order = account.folder_order or []
    ordered_ids: list[uuid.UUID] = []
    for fid_str in order:
        try:
            fid = uuid.UUID(str(fid_str))
            if fid in folder_map:
                ordered_ids.append(fid)
        except ValueError:
            continue

    # Append any folders not in the custom order
    remaining = [fid for fid in folder_map if fid not in ordered_ids]
    remaining.sort(key=lambda fid: folder_map[fid][0].imap_name)
    ordered_ids.extend(remaining)

    items = []
    for fid in ordered_ids:
        f, total, unread = folder_map[fid]
        items.append(
            FolderOrderItem(
                folder_id=f.id,
                imap_name=f.imap_name,
                display_name=f.display_name,
                special_use=f.special_use.value if f.special_use else None,
                is_visible=f.is_visible,
                unread_count=unread,
                total_count=total,
            )
        )

    return FolderOrderResponse(folders=items)


@router.put("/folder-order", response_model=FolderOrderResponse)
async def update_folder_order(
    account_id: uuid.UUID,
    request: FolderOrderUpdate,
) -> FolderOrderResponse:
    """Save custom folder display order."""
    await _get_account_or_404(account_id)

    order_strs = [str(fid) for fid in request.order]
    db = get_db_connection()
    async with db.session() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(folder_order=order_strs)
        )

    return await get_folder_order(account_id)


@router.patch(
    "/folders/{folder_id}/visibility",
    response_model=FolderVisibilityResponse,
)
async def toggle_folder_visibility(
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    request: FolderVisibilityUpdate,
) -> FolderVisibilityResponse:
    """Toggle visibility for a folder."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.account_id == account_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Folder not found")

        await session.execute(
            update(Folder)
            .where(Folder.id == folder_id)
            .values(is_visible=request.is_visible)
        )

    return FolderVisibilityResponse(
        folder_id=folder_id,
        is_visible=request.is_visible,
    )


# --- Folder Assignment (auto-detect) ---


@router.post("/folder-mapping/auto-detect")
async def auto_detect_folder_mapping(
    account_id: uuid.UUID,
) -> dict[str, str | None]:
    """Re-run folder auto-detection from IMAP special-use flags and name matching."""
    from mail_verdict.sync.folder_mapping import auto_detect_mapping

    await _get_account_or_404(account_id)

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(Folder.account_id == account_id)
        )
        folders = list(result.scalars().all())

    folder_dicts: list[dict[str, str | None]] = [
        {
            "imap_name": f.imap_name,
            "special_use": f.special_use.value if f.special_use else None,
        }
        for f in folders
    ]
    return auto_detect_mapping(folder_dicts)


# --- IDLE Configuration ---


@router.get("/idle-folders", response_model=list[IdleFolderItem])
async def get_idle_folders(account_id: uuid.UUID) -> list[IdleFolderItem]:
    """List all folders with their IDLE enabled status."""
    account = await _get_account_or_404(account_id)
    idle_set = set(account.idle_folders or [])

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder)
            .where(Folder.account_id == account_id)
            .order_by(Folder.imap_name)
        )
        folders = list(result.scalars().all())

    return [
        IdleFolderItem(
            folder_id=f.id,
            imap_name=f.imap_name,
            idle_enabled=str(f.id) in idle_set,
        )
        for f in folders
    ]


@router.put("/idle-folders", response_model=IdleFolderToggleResponse)
async def toggle_idle_folder(
    account_id: uuid.UUID,
    request: IdleFolderToggle,
) -> IdleFolderToggleResponse:
    """Enable or disable IDLE for a specific folder."""
    account = await _get_account_or_404(account_id)

    # Verify folder exists and belongs to account
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(
                Folder.id == request.folder_id,
                Folder.account_id == account_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Folder not found")

    idle_set = set(account.idle_folders or [])
    fid_str = str(request.folder_id)

    if request.enabled:
        idle_set.add(fid_str)
    else:
        idle_set.discard(fid_str)

    async with db.session() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(idle_folders=list(idle_set))
        )

    return IdleFolderToggleResponse(
        folder_id=request.folder_id,
        enabled=request.enabled,
        success=True,
    )


@router.post("/validate-idle", response_model=IdleValidationResponse)
async def validate_idle(
    account_id: uuid.UUID,
    request: IdleValidationRequest,
) -> IdleValidationResponse:
    """Test if a folder supports IMAP IDLE."""
    from mail_verdict.core.encryption import decrypt

    account = await _get_account_or_404(account_id)

    # Get folder IMAP name
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(
                Folder.id == request.folder_id,
                Folder.account_id == account_id,
            )
        )
        folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    password = decrypt(account.imap_password) if account.imap_password else ""
    use_ssl = account.imap_port in (993, 995)

    def _check_idle() -> tuple[bool, str | None]:
        """Test IDLE support on the folder via imap-tools."""
        try:
            from imap_tools import (
                BaseMailBox,
                MailBox,
                MailBoxStartTls,
                MailBoxUnencrypted,
            )

            mb: BaseMailBox
            if use_ssl:
                mb = MailBox(account.imap_host, account.imap_port)
            elif account.imap_port == 143:
                mb = MailBoxStartTls(account.imap_host, account.imap_port)
            else:
                mb = MailBoxUnencrypted(account.imap_host, account.imap_port)

            mb.login(account.imap_user, password, initial_folder=folder.imap_name)

            # Check CAPABILITY for IDLE support
            caps = mb.client.capabilities
            has_idle = b"IDLE" in caps if caps else False

            mb.logout()
            if has_idle:
                return True, None
            return False, "Server does not support IDLE"
        except Exception as exc:
            return False, str(exc)

    try:
        supported, error = await asyncio.wait_for(
            asyncio.to_thread(_check_idle),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        supported = False
        error = "IDLE validation timed out"

    return IdleValidationResponse(
        folder_id=request.folder_id,
        supported=supported,
        error=error,
    )
