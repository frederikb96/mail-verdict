"""
Folder management API endpoints.

GET/PUT /accounts/{account_id}/folder-order — custom folder display order
PATCH /folders/{folder_id}/prefs — the one write surface a folder has:
  visibility, display name, unified name, special-use override

IDLE endpoints removed -- PostIMAP handles IMAP IDLE. Folder create/rename/
delete is a documented PostIMAP non-goal; there is no such surface here.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import case, select
from sqlalchemy import func as sa_func

from mail_verdict.api.deps import get_account_prefs_repo, get_folder_prefs_repo
from mail_verdict.api.schemas import (
    FolderOrderItem,
    FolderOrderResponse,
    FolderOrderUpdate,
    FolderPrefsUpdate,
    FolderResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, FolderPrefs, Message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts/{account_id}", tags=["folder-management"])
folder_prefs_router = APIRouter(prefix="/folders/{folder_id}", tags=["folder-management"])


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
) -> list[tuple[Folder, FolderPrefs | None, int, int]]:
    """Get all folders for an account with prefs and unread/total counts."""
    db = get_db_connection()
    async with db.session() as session:
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
            .where(Folder.account_id == account_id)
            .group_by(Folder.id, FolderPrefs.folder_id)
        )
        result = await session.execute(stmt)
        return list(result.all())  # type: ignore[arg-type]


# --- Folder Ordering ---


@router.get("/folder-order", response_model=FolderOrderResponse)
async def get_folder_order(account_id: uuid.UUID) -> FolderOrderResponse:
    """Get ordered folder list with visibility and counts."""
    await _get_account_or_404(account_id)
    rows = await _get_folders_with_counts(account_id)

    # Get folder order from AccountPrefs
    prefs_repo = get_account_prefs_repo()
    acct_prefs = await prefs_repo.get_by_account(account_id)
    order = (acct_prefs.folder_order if acct_prefs else None) or []

    # Build lookup by folder ID
    folder_map: dict[uuid.UUID, tuple[Folder, FolderPrefs | None, int, int]] = {
        f.id: (f, fp, total, unread) for f, fp, total, unread in rows
    }

    # Apply custom order if set, otherwise alphabetical
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
        f, fp, total, unread = folder_map[fid]
        items.append(
            FolderOrderItem(
                folder_id=f.id,
                imap_name=f.imap_name,
                display_name=f.display_name or (fp.display_name if fp else None),
                special_use=f.special_use,
                is_visible=fp.is_visible if fp else True,
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
    """Save custom folder display order in AccountPrefs."""
    await _get_account_or_404(account_id)

    order_strs = [str(fid) for fid in request.order]
    prefs_repo = get_account_prefs_repo()
    await prefs_repo.update(account_id, folder_order=order_strs)

    return await get_folder_order(account_id)


# --- Folder preferences ---


@folder_prefs_router.patch("/prefs", response_model=FolderResponse)
async def update_folder_prefs(
    folder_id: uuid.UUID,
    request: FolderPrefsUpdate,
) -> FolderResponse:
    """
    Partially update a folder's MailVerdict preferences.

    The one write surface for a folder's preferences: visibility, display
    name, unified name, and special-use override.
    """
    values = request.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")

    prefs_repo = get_folder_prefs_repo()
    await prefs_repo.update(folder_id, **values)

    db = get_db_connection()
    async with db.session() as session:
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
            .where(Folder.id == folder_id)
            .group_by(Folder.id, FolderPrefs.folder_id)
        )
        result = await session.execute(stmt)
        row = result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    f, fp, total, unread = row
    return FolderResponse(
        id=f.id,
        account_id=f.account_id,
        imap_name=f.imap_name,
        display_name=f.display_name or (fp.display_name if fp else None),
        special_use=(fp.special_use_override if fp else None) or f.special_use,
        mailbox_id=f.mailbox_id,
        initial_sync_done=f.initial_sync_done,
        last_synced_at=f.last_synced_at,
        sync_error=f.sync_error,
        created_at=f.created_at,
        unified_name=fp.unified_name if fp else None,
        is_visible=fp.is_visible if fp else True,
        total_count=total,
        unread_count=unread,
    )
