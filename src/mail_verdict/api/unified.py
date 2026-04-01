"""
Unified view API endpoints.

Multi-account folder merging and cross-account message listing.

PUT /api/accounts/:id/emoji — set account emoji (via AccountPrefs)
PUT /api/accounts/:id/folders/:fid/unified-name — set folder unified name (via FolderPrefs)
GET /api/unified/folders — merged folder list across all accounts
GET /api/unified/mails — merged message list sorted by date
GET /api/unified/folder-order — unified folder display order
PUT /api/unified/folder-order — save unified folder display order
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, case, desc, or_, select, update
from sqlalchemy import func as sa_func

from mail_verdict.api.deps import get_account_prefs_repo, get_folder_prefs_repo
from mail_verdict.api.schemas import (
    EmojiUpdate,
    FolderResponse,
    UnifiedFolderOrderResponse,
    UnifiedFolderOrderUpdate,
    UnifiedFolderResponse,
    UnifiedFolderSource,
    UnifiedMessageListResponse,
    UnifiedMessageSummary,
    UnifiedNameUpdate,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Folder,
    FolderPrefs,
    Message,
    Setting,
)

logger = logging.getLogger(__name__)

UNIFIED_VIEW_CATEGORY = "unified_view"

# Account-scoped endpoints for emoji and unified name
account_router = APIRouter(prefix="/accounts/{account_id}", tags=["unified-view"])

# Top-level unified endpoints
unified_router = APIRouter(prefix="/unified", tags=["unified-view"])


# --- Account-scoped configuration ---


@account_router.put("/emoji")
async def set_account_emoji(
    account_id: uuid.UUID,
    request: EmojiUpdate,
) -> dict[str, str | None]:
    """Set the emoji icon for an account (stored in AccountPrefs)."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

    prefs_repo = get_account_prefs_repo()
    await prefs_repo.update(account_id, emoji=request.emoji)

    return {"emoji": request.emoji}


@account_router.put(
    "/folders/{folder_id}/unified-name",
    response_model=FolderResponse,
)
async def set_unified_name(
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    request: UnifiedNameUpdate,
) -> FolderResponse:
    """Set or clear the unified name for a folder (stored in FolderPrefs)."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.account_id == account_id,
            )
        )
        folder = result.scalar_one_or_none()
        if folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")

    # Update FolderPrefs
    prefs_repo = get_folder_prefs_repo()
    await prefs_repo.update(folder_id, unified_name=request.unified_name)

    # Re-fetch with counts to return FolderResponse
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
                (Message.folder_id == Folder.id) & Message.deleted_at.is_(None),
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


# --- Unified data queries ---


@unified_router.get("/folders", response_model=list[UnifiedFolderResponse])
async def list_unified_folders() -> list[UnifiedFolderResponse]:
    """
    List merged folders across all active accounts.

    Groups folders by unified_name (from FolderPrefs) and aggregates counts.
    Ordered by stored folder order, with unordered folders appended.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(
                Folder,
                FolderPrefs,
                Account.name.label("account_name"),
                AccountPrefs.emoji.label("account_emoji"),
                sa_func.count(Message.id).label("total_count"),
                sa_func.count(
                    case((Message.is_seen.is_(False), Message.id))
                ).label("unread_count"),
            )
            .join(Account, Folder.account_id == Account.id)
            .join(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .outerjoin(
                Message,
                (Message.folder_id == Folder.id) & Message.deleted_at.is_(None),
            )
            .where(
                FolderPrefs.unified_name.isnot(None),
                FolderPrefs.unified_name != "",
                Account.is_active.is_(True),
            )
            .group_by(
                Folder.id, FolderPrefs.folder_id,
                Account.name, AccountPrefs.emoji,
            )
        )
        result = await session.execute(stmt)
        rows = list(result.all())

    # Group by unified_name
    groups: dict[str, dict[str, Any]] = {}
    for folder, fp, account_name, account_emoji, total, unread in rows:
        name = fp.unified_name
        if name not in groups:
            groups[name] = {
                "unified_name": name,
                "folders": [],
                "unread_count": 0,
                "total_count": 0,
            }
        groups[name]["folders"].append(
            UnifiedFolderSource(
                account_id=folder.account_id,
                account_name=account_name,
                account_emoji=account_emoji,
                folder_id=folder.id,
                imap_name=folder.imap_name,
            )
        )
        groups[name]["unread_count"] += unread
        groups[name]["total_count"] += total

    # Apply stored order
    order = await _get_folder_order()
    result_list: list[UnifiedFolderResponse] = []
    seen: set[str] = set()

    for name in order:
        if name in groups:
            result_list.append(UnifiedFolderResponse(**groups[name]))
            seen.add(name)

    # Append remaining folders alphabetically
    for name in sorted(groups.keys()):
        if name not in seen:
            result_list.append(UnifiedFolderResponse(**groups[name]))

    return result_list


@unified_router.get("/mails", response_model=UnifiedMessageListResponse)
async def list_unified_messages(
    folder_name: str = Query(description="Unified folder name to list messages from"),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last message in previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> UnifiedMessageListResponse:
    """
    List messages from all folders matching a unified name, sorted by date.

    Cross-account cursor-based pagination.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(Message, AccountPrefs.emoji.label("account_emoji"))
            .join(Folder, Message.folder_id == Folder.id)
            .join(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .join(Account, Message.account_id == Account.id)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .where(
                FolderPrefs.unified_name == folder_name,
                Account.is_active.is_(True),
                Message.deleted_at.is_(None),
            )
            .order_by(desc(Message.received_at), desc(Message.id))
        )

        # Cursor-based pagination
        if before is not None:
            cursor_result = await session.execute(
                select(Message.received_at, Message.id).where(Message.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: message {before} not found",
                )
            cursor_received_at, cursor_id = cursor_row
            stmt = stmt.where(
                or_(
                    Message.received_at < cursor_received_at,
                    and_(
                        Message.received_at == cursor_received_at,
                        Message.id < cursor_id,
                    ),
                )
            )

        stmt = stmt.limit(limit + 1)
        result = await session.execute(stmt)
        rows = list(result.all())

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = str(rows[-1][0].id) if has_more and rows else None

    messages = [
        UnifiedMessageSummary(
            id=msg.id,
            account_id=msg.account_id,
            account_emoji=account_emoji,
            folder_id=msg.folder_id,
            subject=msg.subject,
            from_addr=msg.from_addr,
            to_addrs=msg.to_addrs,
            received_at=msg.received_at,
            is_seen=msg.is_seen,
            is_flagged=msg.is_flagged,
            is_answered=msg.is_answered,
            is_draft=msg.is_draft,
            is_deleted=msg.is_deleted,
            deleted_at=msg.deleted_at,
            snippet=msg.body_text[:120] if msg.body_text else None,
        )
        for msg, account_emoji in rows
    ]

    return UnifiedMessageListResponse(
        messages=messages,
        has_more=has_more,
        next_cursor=next_cursor,
    )


# --- Unified folder order ---


async def _get_folder_order() -> list[str]:
    """Read unified folder order from the settings table."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Setting).where(Setting.category == UNIFIED_VIEW_CATEGORY)
        )
        setting = result.scalar_one_or_none()

    if setting and isinstance(setting.data, dict):
        order: list[str] = setting.data.get("folder_order", [])
        return order
    return []


@unified_router.get("/folder-order", response_model=UnifiedFolderOrderResponse)
async def get_unified_folder_order() -> UnifiedFolderOrderResponse:
    """Get the unified folder display order."""
    order = await _get_folder_order()
    return UnifiedFolderOrderResponse(order=order)


@unified_router.put("/folder-order", response_model=UnifiedFolderOrderResponse)
async def set_unified_folder_order(
    request: UnifiedFolderOrderUpdate,
) -> UnifiedFolderOrderResponse:
    """Save the unified folder display order."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Setting).where(Setting.category == UNIFIED_VIEW_CATEGORY)
        )
        existing = result.scalar_one_or_none()

        if existing:
            data = dict(existing.data) if existing.data else {}
            data["folder_order"] = request.order
            await session.execute(
                update(Setting)
                .where(Setting.category == UNIFIED_VIEW_CATEGORY)
                .values(data=data)
            )
        else:
            session.add(
                Setting(
                    category=UNIFIED_VIEW_CATEGORY,
                    data={"folder_order": request.order},
                )
            )

    return UnifiedFolderOrderResponse(order=request.order)
