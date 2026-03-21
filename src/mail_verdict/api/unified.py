"""
Unified view API endpoints.

Multi-account folder merging and cross-account mail listing.

PUT /api/accounts/:id/emoji — set account emoji
PUT /api/accounts/:id/folders/:fid/unified-name — set folder unified name
GET /api/unified/folders — merged folder list across all accounts
GET /api/unified/mails — merged mail list sorted by date
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

from mail_verdict.api.schemas import (
    EmojiUpdate,
    FolderResponse,
    UnifiedFolderOrderResponse,
    UnifiedFolderOrderUpdate,
    UnifiedFolderResponse,
    UnifiedFolderSource,
    UnifiedMailListResponse,
    UnifiedMailSummary,
    UnifiedNameUpdate,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, Mail, Setting

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
    """Set the emoji icon for an account."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(emoji=request.emoji)
        )

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
    """Set or clear the unified name for a folder."""
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

        await session.execute(
            update(Folder)
            .where(Folder.id == folder_id)
            .values(unified_name=request.unified_name)
        )

    # Re-fetch with counts to return FolderResponse
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
            .where(Folder.id == folder_id)
            .group_by(Folder.id)
        )
        result = await session.execute(stmt)
        row = result.one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    f, total, unread = row
    return FolderResponse(
        id=f.id,
        account_id=f.account_id,
        imap_name=f.imap_name,
        display_name=f.display_name,
        special_use=f.special_use.value if f.special_use else None,
        unified_name=f.unified_name,
        subscribed=f.subscribed,
        is_visible=f.is_visible,
        last_synced_at=f.last_synced_at,
        total_count=total,
        unread_count=unread,
    )


# --- Unified data queries ---


@unified_router.get("/folders", response_model=list[UnifiedFolderResponse])
async def list_unified_folders() -> list[UnifiedFolderResponse]:
    """
    List merged folders across all active accounts.

    Groups folders by unified_name and aggregates counts.
    Ordered by stored folder order, with unordered folders appended.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(
                Folder,
                Account.name.label("account_name"),
                Account.emoji.label("account_emoji"),
                sa_func.count(Mail.id).label("total_count"),
                sa_func.count(
                    case((Mail.is_read.is_(False), Mail.id))
                ).label("unread_count"),
            )
            .join(Account, Folder.account_id == Account.id)
            .outerjoin(
                Mail,
                (Mail.folder_id == Folder.id) & Mail.is_deleted.is_(False),
            )
            .where(
                Folder.unified_name.isnot(None),
                Folder.unified_name != "",
                Account.is_active.is_(True),
            )
            .group_by(Folder.id, Account.name, Account.emoji)
        )
        result = await session.execute(stmt)
        rows = list(result.all())

    # Group by unified_name
    groups: dict[str, dict[str, Any]] = {}
    for folder, account_name, account_emoji, total, unread in rows:
        name = folder.unified_name
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


@unified_router.get("/mails", response_model=UnifiedMailListResponse)
async def list_unified_mails(
    folder_name: str = Query(description="Unified folder name to list mails from"),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last mail in previous page",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> UnifiedMailListResponse:
    """
    List mails from all folders matching a unified name, sorted by date.

    Cross-account cursor-based pagination.
    """
    db = get_db_connection()
    async with db.session() as session:
        stmt = (
            select(Mail, Account.emoji.label("account_emoji"))
            .join(Folder, Mail.folder_id == Folder.id)
            .join(Account, Mail.account_id == Account.id)
            .where(
                Folder.unified_name == folder_name,
                Account.is_active.is_(True),
                Mail.is_deleted.is_(False),
            )
            .order_by(desc(Mail.received_at), desc(Mail.id))
        )

        # Cursor-based pagination
        if before is not None:
            cursor_result = await session.execute(
                select(Mail.received_at, Mail.id).where(Mail.id == before)
            )
            cursor_row = cursor_result.one_or_none()
            if cursor_row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid cursor: mail {before} not found",
                )
            cursor_received_at, cursor_id = cursor_row
            stmt = stmt.where(
                or_(
                    Mail.received_at < cursor_received_at,
                    and_(
                        Mail.received_at == cursor_received_at,
                        Mail.id < cursor_id,
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

    mails = [
        UnifiedMailSummary(
            id=mail.id,
            account_id=mail.account_id,
            account_emoji=account_emoji,
            folder_id=mail.folder_id,
            subject=mail.subject,
            from_addr=mail.from_addr,
            to_addrs=mail.to_addrs,
            received_at=mail.received_at,
            is_read=mail.is_read,
            is_flagged=mail.is_flagged,
            is_deleted=mail.is_deleted,
            headers_synced=mail.headers_synced,
            body_synced=mail.body_synced,
        )
        for mail, account_emoji in rows
    ]

    return UnifiedMailListResponse(
        mails=mails,
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
