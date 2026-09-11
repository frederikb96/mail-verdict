"""
Unified view API endpoints.

A unified view merges any set of folders, across accounts, into one mail
list. Views live in unified_views; which folders each one shows lives in
unified_view_folders, and one folder may belong to several views.

PUT /api/accounts/:id/emoji — set account emoji (via AccountPrefs)
GET /api/unified/folders — every view with its member folders and counts
POST /api/unified/views — create a view
PATCH /api/unified/views/:id — rename a view, or set or clear its emoji
DELETE /api/unified/views/:id — delete a view (its folders and mail are untouched)
GET /api/unified/mails — one view's messages, paged, threaded and filtered
  exactly as an account's own list is
GET /api/unified/folder-order — view display order, by name
PUT /api/unified/folder-order — save view display order

A folder's own memberships are set via PATCH /folders/{folder_id}/prefs
(folder_management.py) -- one write surface for every folder preference.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, case, delete, insert, select, text
from sqlalchemy import func as sa_func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.deps import get_account_prefs_repo
from mail_verdict.api.events import broadcast_event, get_event_ring
from mail_verdict.api.mails import list_message_page
from mail_verdict.api.schemas import (
    EmojiUpdate,
    MessageListResponse,
    UnifiedFolderOrderResponse,
    UnifiedFolderOrderUpdate,
    UnifiedFolderResponse,
    UnifiedFolderSource,
    UnifiedViewCreate,
    UnifiedViewResponse,
    UnifiedViewUpdate,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Folder,
    FolderPrefs,
    Message,
    UnifiedView,
    UnifiedViewFolder,
)

logger = logging.getLogger(__name__)

# Account-scoped endpoint for emoji
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

    # AccountPrefs is MailVerdict's own table -- see api/accounts.py's
    # update_account() for the same reasoning on its own emoji/spam_enabled
    # patch path.
    event_ring = get_event_ring()
    if event_ring is not None:
        await event_ring.add(account_id, "account.changed", {"id": str(account_id), "op": "update"})

    return {"emoji": request.emoji}


# --- Membership, shared with folder_management.py and accounts.py ---


def member_folder_ids(view_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """The folders a view actually shows: its members on an active account
    that have not been deleted. The one definition every read of a view's
    contents goes through."""
    return (
        select(UnifiedViewFolder.folder_id)
        .join(Folder, Folder.id == UnifiedViewFolder.folder_id)
        .join(Account, Account.id == Folder.account_id)
        .where(
            UnifiedViewFolder.view_id == view_id,
            Folder.deleted_at.is_(None),
            Account.is_active.is_(True),
        )
    )


async def view_ids_by_folder(
    session: AsyncSession, folder_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Every view each of these folders belongs to, in sidebar order."""
    if not folder_ids:
        return {}
    result = await session.execute(
        select(UnifiedViewFolder.folder_id, UnifiedViewFolder.view_id)
        .join(UnifiedView, UnifiedView.id == UnifiedViewFolder.view_id)
        .where(UnifiedViewFolder.folder_id.in_(folder_ids))
        .order_by(UnifiedView.position, UnifiedView.name)
    )
    by_folder: dict[uuid.UUID, list[uuid.UUID]] = {}
    for folder_id, view_id in result.all():
        by_folder.setdefault(folder_id, []).append(view_id)
    return by_folder


async def set_folder_views(
    session: AsyncSession, folder_id: uuid.UUID, view_ids: Sequence[uuid.UUID],
) -> None:
    """Make view_ids the complete set of views this folder belongs to.

    404 for a folder that does not exist (or was deleted) or a view id
    that names no view -- checked before anything is written, so a bad
    request changes nothing."""
    # The multi-select saves on every tick, so two writes for one folder can
    # overlap. Held until this transaction commits, the lock has each one
    # replace the whole set in turn; otherwise the later insert collides
    # with rows the earlier one committed after this delete had already run.
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtext('unified_view_folders'), hashtext(:folder))"
        ),
        {"folder": str(folder_id)},
    )
    folder_exists = await session.scalar(
        select(Folder.id).where(Folder.id == folder_id, Folder.deleted_at.is_(None))
    )
    if folder_exists is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    wanted = list(dict.fromkeys(view_ids))
    if wanted:
        found = set(
            (await session.execute(
                select(UnifiedView.id).where(UnifiedView.id.in_(wanted))
            )).scalars()
        )
        missing = [v for v in wanted if v not in found]
        if missing:
            raise HTTPException(status_code=404, detail=f"Unified view {missing[0]} not found")

    await session.execute(
        delete(UnifiedViewFolder).where(UnifiedViewFolder.folder_id == folder_id)
    )
    if wanted:
        await session.execute(
            insert(UnifiedViewFolder),
            [{"view_id": v, "folder_id": folder_id} for v in wanted],
        )


async def announce_views_changed() -> None:
    """Views are not account-scoped, so there is no single account to key
    the event on -- broadcast to every account's ring (see
    broadcast_event). folder.changed, since the client already refreshes
    every folder-related cache, the unified ones included, on it."""
    event_ring = get_event_ring()
    if event_ring is not None:
        await broadcast_event(get_db_connection(), event_ring, "folder.changed", {})


# --- Views ---


@unified_router.get("/folders", response_model=list[UnifiedFolderResponse])
async def list_unified_folders() -> list[UnifiedFolderResponse]:
    """
    Every unified view, in sidebar order, with its member folders and their
    summed counts. A folder belonging to two views counts in both.
    """
    db = get_db_connection()
    async with db.session() as session:
        views = list(
            (await session.execute(
                select(UnifiedView).order_by(UnifiedView.position, UnifiedView.name)
            )).scalars().all()
        )
        stmt = (
            select(
                UnifiedViewFolder.view_id,
                Folder,
                FolderPrefs.special_use_override,
                Account.name.label("account_name"),
                AccountPrefs.emoji.label("account_emoji"),
                sa_func.count(Message.id).label("total_count"),
                sa_func.count(
                    case((Message.is_seen.is_(False), Message.id))
                ).label("unread_count"),
            )
            .select_from(UnifiedViewFolder)
            .join(Folder, Folder.id == UnifiedViewFolder.folder_id)
            .join(Account, Folder.account_id == Account.id)
            .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .outerjoin(AccountPrefs, Account.id == AccountPrefs.account_id)
            .outerjoin(
                Message,
                (Message.folder_id == Folder.id) & Message.expunged_at.is_(None),
            )
            .where(Account.is_active.is_(True), Folder.deleted_at.is_(None))
            .group_by(
                UnifiedViewFolder.view_id, Folder.id, FolderPrefs.special_use_override,
                Account.name, AccountPrefs.emoji,
            )
            .order_by(Account.name, Folder.imap_name)
        )
        rows = list((await session.execute(stmt)).all())

    members: dict[uuid.UUID, list[tuple[UnifiedFolderSource, int, int]]] = {}
    for view_id, folder, override, account_name, account_emoji, total, unread in rows:
        members.setdefault(view_id, []).append((
            UnifiedFolderSource(
                account_id=folder.account_id,
                account_name=account_name,
                account_emoji=account_emoji,
                folder_id=folder.id,
                imap_name=folder.imap_name,
                special_use=override or folder.special_use,
            ),
            total,
            unread,
        ))

    return [
        UnifiedFolderResponse(
            id=view.id,
            unified_name=view.name,
            emoji=view.emoji,
            folders=[source for source, _, _ in members.get(view.id, [])],
            total_count=sum(total for _, total, _ in members.get(view.id, [])),
            unread_count=sum(unread for _, _, unread in members.get(view.id, [])),
        )
        for view in views
    ]


def _view_response(view: UnifiedView) -> UnifiedViewResponse:
    return UnifiedViewResponse(
        id=view.id, name=view.name, emoji=view.emoji, position=view.position,
    )


def _clean_emoji(emoji: str | None) -> str | None:
    return emoji.strip() or None if emoji is not None else None


@unified_router.post("/views", response_model=UnifiedViewResponse, status_code=201)
async def create_unified_view(request: UnifiedViewCreate) -> UnifiedViewResponse:
    """Create an empty view, placed last in the sidebar. 409 if the name is
    already taken -- a view is addressed by its name in the mail URL."""
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A unified view needs a name")

    db = get_db_connection()
    async with db.session() as session:
        last = await session.scalar(select(sa_func.max(UnifiedView.position)))
        view = UnifiedView(
            id=uuid.uuid4(), name=name, emoji=_clean_emoji(request.emoji),
            position=(last + 1) if last is not None else 0,
        )
        session.add(view)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail=f"A unified view named {name!r} already exists",
            ) from exc
        response = _view_response(view)

    await announce_views_changed()
    return response


@unified_router.patch("/views/{view_id}", response_model=UnifiedViewResponse)
async def update_unified_view(
    view_id: uuid.UUID, request: UnifiedViewUpdate,
) -> UnifiedViewResponse:
    """Rename a view, or set or clear its emoji (an explicit null)."""
    values = request.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "name" in values:
        name = (values["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="A unified view needs a name")
        values["name"] = name
    if "emoji" in values:
        values["emoji"] = _clean_emoji(values["emoji"])

    db = get_db_connection()
    async with db.session() as session:
        view = await session.get(UnifiedView, view_id)
        if view is None:
            raise HTTPException(status_code=404, detail="Unified view not found")
        for key, value in values.items():
            setattr(view, key, value)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"A unified view named {values.get('name')!r} already exists",
            ) from exc
        response = _view_response(view)

    await announce_views_changed()
    return response


@unified_router.delete("/views/{view_id}", status_code=204)
async def delete_unified_view(view_id: uuid.UUID) -> None:
    """Delete a view. Only the grouping goes: its folders and every message
    in them are untouched, as is their membership of any other view."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(delete(UnifiedView).where(UnifiedView.id == view_id))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise HTTPException(status_code=404, detail="Unified view not found")

    await announce_views_changed()


# --- Messages ---


@unified_router.get("/mails", response_model=MessageListResponse)
async def list_unified_messages(
    folder_name: str = Query(description="Name of the unified view to list"),
    before: uuid.UUID | None = Query(
        default=None,
        description="Cursor: UUID of last message in previous page -- fetches older",
    ),
    limit: int = Query(
        default=50, ge=1, le=1000,
        description=(
            "Rows per page. The ceiling is high enough for a client to re-read "
            "everything it has loaded in one request rather than page by page."
        ),
    ),
    # Annotated, not `= Query(default=...)` -- tests in tests/pg call this
    # function directly, where a bare Query default would arrive as a
    # truthy descriptor object rather than its default value.
    threaded: Annotated[bool, Query(description="One row per conversation")] = False,
    is_seen: Annotated[bool | None, Query(description="Only read, or only unread")] = None,
    after: Annotated[
        uuid.UUID | None,
        Query(description="Cursor: UUID of first message in previous page -- fetches newer"),
    ] = None,
    around: Annotated[
        uuid.UUID | None,
        Query(description="Centre a fresh page on this message -- see the account list"),
    ] = None,
    since: Annotated[datetime | None, Query()] = None,
) -> MessageListResponse:
    """
    One view's messages, newest first, across every member folder and
    account. Pages, threads (a conversation spanning two member folders is
    one row) and filters exactly as GET /accounts/{id}/messages does --
    both go through the same list code. 404 for a view name that does not
    exist.
    """
    db = get_db_connection()
    async with db.session() as session:
        view_id = await session.scalar(
            select(UnifiedView.id).where(UnifiedView.name == folder_name)
        )
        if view_id is None:
            raise HTTPException(status_code=404, detail="Unified view not found")
        return await list_message_page(
            session, account_id=None, folder_id=None,
            folder_scope=member_folder_ids(view_id),
            threaded=threaded, is_seen=is_seen, since=since,
            before=before, after=after, around=around, limit=limit,
        )


# --- View order ---


async def _names_in_order(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(UnifiedView.name).order_by(UnifiedView.position, UnifiedView.name)
    )
    return list(result.scalars().all())


@unified_router.get("/folder-order", response_model=UnifiedFolderOrderResponse)
async def get_unified_folder_order() -> UnifiedFolderOrderResponse:
    """The views' sidebar order, by name."""
    db = get_db_connection()
    async with db.session() as session:
        return UnifiedFolderOrderResponse(order=await _names_in_order(session))


@unified_router.put("/folder-order", response_model=UnifiedFolderOrderResponse)
async def set_unified_folder_order(
    request: UnifiedFolderOrderUpdate,
) -> UnifiedFolderOrderResponse:
    """Save the views' sidebar order. Names that match no view are ignored;
    a view the request leaves out keeps its relative place after the ones
    it names."""
    db = get_db_connection()
    async with db.session() as session:
        views = {
            v.name: v
            for v in (await session.execute(
                select(UnifiedView).order_by(UnifiedView.position, UnifiedView.name)
            )).scalars().all()
        }
        named = [n for n in dict.fromkeys(request.order) if n in views]
        rest = [n for n in views if n not in set(named)]
        ordered: list[Any] = [*named, *rest]
        for position, name in enumerate(ordered):
            views[name].position = position
        await session.flush()
        order = await _names_in_order(session)

    await announce_views_changed()
    return UnifiedFolderOrderResponse(order=order)
