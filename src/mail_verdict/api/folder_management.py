"""
Folder management API endpoints.

GET/PUT /accounts/{account_id}/folder-order — custom folder display order
PATCH /folders/{folder_id}/prefs — a folder's write surface: visibility,
  display name, unified name, special-use override, and real-time sync
POST /accounts/{account_id}/folders — create a folder (requires PostIMAP >= 1.3.0)
DELETE /folders/{folder_id} — delete a folder and every message in it on the
  server, irreversibly (requires PostIMAP >= 1.3.0)

Renaming and re-nesting a folder is a documented PostIMAP non-goal and
stays out here too: IMAP RENAME also renames every child folder, which no
single-row UPDATE can express.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import case, select
from sqlalchemy import func as sa_func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.deps import get_account_prefs_repo, get_folder_prefs_repo
from mail_verdict.api.schemas import (
    FolderCreateRequest,
    FolderOrderItem,
    FolderOrderResponse,
    FolderOrderUpdate,
    FolderPrefsUpdate,
    FolderResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder, FolderPrefs, Message
from mail_verdict.postimap.actions import create_folder as postimap_create_folder
from mail_verdict.postimap.actions import delete_folder as postimap_delete_folder
from mail_verdict.postimap.actions import set_folder_idle
from mail_verdict.postimap.contract import read_postimap_info, supports_folder_crud

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts/{account_id}", tags=["folder-management"])
folder_prefs_router = APIRouter(prefix="/folders/{folder_id}", tags=["folder-management"])

_FOLDER_CRUD_UNSUPPORTED_DETAIL = (
    "Folder creation and deletion require PostIMAP service_version >= 1.3.0; "
    "the running instance reports {version}."
)


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
            # A deleted folder is tombstoned rather than removed, so every
            # listing has to exclude it or it lingers in the sidebar forever.
            .where(Folder.account_id == account_id, Folder.deleted_at.is_(None))
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


async def _fetch_folder_response(
    session: AsyncSession, folder_id: uuid.UUID,
) -> FolderResponse | None:
    """Re-select one folder with its prefs and live message counts."""
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
        return None

    f, fp, total, unread = row
    return FolderResponse(
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


async def _require_folder_crud_support(session: AsyncSession) -> None:
    """Raise 501 unless the running PostIMAP grants folder creation/deletion."""
    info = await read_postimap_info(session)
    if info is None or not supports_folder_crud(info):
        raise HTTPException(
            status_code=501,
            detail=_FOLDER_CRUD_UNSUPPORTED_DETAIL.format(
                version=info.service_version if info else "unknown",
            ),
        )


# --- Folder creation and deletion ---


@router.post("/folders", response_model=FolderResponse, status_code=201)
async def create_folder(
    account_id: uuid.UUID,
    request: FolderCreateRequest,
) -> FolderResponse:
    """
    Create a folder.

    IMAP has no parent concept, so parent_id (when given) is resolved to
    its imap_name and the new folder's full path is built by joining onto
    it with the account's own separator. Requires PostIMAP >= 1.3.0.

    A folder that already exists on the server but not yet in this mirror
    (because it was just created outside this app, say) is created here
    without error -- PostIMAP's own CREATE against an existing mailbox is a
    no-op success. A path that is already a *live* folder in this mirror
    is rejected with 409: PostIMAP's own unique index allows only one
    live row per (account_id, imap_name).
    """
    db = get_db_connection()
    async with db.session() as session:
        await _require_folder_crud_support(session)

        account_result = await session.execute(select(Account).where(Account.id == account_id))
        if account_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        if request.parent_id is not None:
            parent_result = await session.execute(
                select(Folder).where(
                    Folder.id == request.parent_id,
                    Folder.account_id == account_id,
                    Folder.deleted_at.is_(None),
                )
            )
            parent = parent_result.scalar_one_or_none()
            if parent is None:
                raise HTTPException(status_code=404, detail="Parent folder not found")

            separator = parent.separator
            if separator is None:
                sep_result = await session.execute(
                    select(Folder.separator)
                    .where(Folder.account_id == account_id, Folder.separator.is_not(None))
                    .limit(1)
                )
                separator = sep_result.scalar_one_or_none()
            if separator is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This account has not completed a sync yet, so its "
                        "folder hierarchy separator is unknown. Try again "
                        "once at least one folder has synced."
                    ),
                )
            imap_name = f"{parent.imap_name}{separator}{request.name}"
        else:
            imap_name = request.name

        try:
            folder_id = await postimap_create_folder(
                session, account_id=account_id, imap_name=imap_name,
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"A folder named {imap_name!r} already exists on this account",
            ) from exc

        response = await _fetch_folder_response(session, folder_id)
        if response is None:
            # Cannot happen: folder_id names the row this same session just
            # inserted and committed.
            raise HTTPException(status_code=500, detail="Folder created but could not be read back")
        return response


@folder_prefs_router.delete("", status_code=204)
async def delete_folder(
    folder_id: uuid.UUID,
    confirm_message_count: int | None = Query(
        default=None,
        description=(
            "Required to actually delete. Omit it (or get it wrong) and the "
            "request fails with a 409 naming the folder's current message "
            "count; repeat the call with that number to confirm."
        ),
    ),
) -> None:
    """
    Delete a folder -- destroys every message in it on the mail server,
    irreversibly. There is no undo, and clearing this back out afterwards
    does not recreate the folder. Requires PostIMAP >= 1.3.0.

    Deleting INBOX (or any folder the server otherwise refuses) is rejected
    up front rather than accepted and silently dead-lettered later.

    There is no UI confirmation dialog at this layer -- an API or MCP
    client would otherwise destroy a folder's mail on the first call with
    nothing standing in the way. confirm_message_count is that dialog's
    REST equivalent: a first call without it (or with a stale count)
    reports what deleting this folder would actually destroy instead of
    doing it, and the caller repeats the call naming that count once it
    has been seen.
    """
    db = get_db_connection()
    async with db.session() as session:
        await _require_folder_crud_support(session)

        folder_result = await session.execute(
            select(Folder, FolderPrefs.special_use_override)
            .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
            .where(Folder.id == folder_id, Folder.deleted_at.is_(None))
        )
        row = folder_result.one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        folder, special_use_override = row
        # IMAP's INBOX is case-insensitive and mandatory on every server
        # regardless of whether SPECIAL-USE is advertised, so imap_name is
        # checked unconditionally rather than only when special_use happens
        # to be set. The override is checked too: a user who has told
        # MailVerdict "this folder is my inbox" on a server that never
        # advertised it gets the same protection as one where the server did.
        effective_special_use = (special_use_override or folder.special_use or "").lower()
        if effective_special_use == "inbox" or folder.imap_name.upper() == "INBOX":
            raise HTTPException(status_code=400, detail="INBOX cannot be deleted")

        message_count = await session.scalar(
            select(sa_func.count(Message.id)).where(
                Message.folder_id == folder_id, Message.expunged_at.is_(None),
            )
        )
        if confirm_message_count != message_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This folder holds {message_count} message(s), all destroyed "
                    "on the mail server irreversibly if deleted. Repeat the request "
                    f"with ?confirm_message_count={message_count} to proceed."
                ),
            )

        await postimap_delete_folder(session, folder_id)
        await session.commit()


# --- Folder preferences ---


@folder_prefs_router.patch("/prefs", response_model=FolderResponse)
async def update_folder_prefs(
    folder_id: uuid.UUID,
    request: FolderPrefsUpdate,
) -> FolderResponse:
    """
    Partially update a folder's preferences.

    Visibility, display name, unified name and special-use override are
    MailVerdict's own. real_time is PostIMAP's: it asks for an IMAP
    connection held open on this folder, so changes arrive in seconds
    rather than on the sync interval.

    Each watched folder costs one connection, plus one for the account
    itself, and providers cap connections per account -- commonly around
    ten, though the figure is rarely published. Five or six watched folders
    is comfortable. Exhausting the cap is visible rather than silent: the
    folder's idle_status becomes "failed" and a notification is written,
    instead of it quietly reverting to interval sync.
    """
    values = request.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")

    db = get_db_connection()
    real_time = values.pop("real_time", None)

    if real_time is not None:
        async with db.session() as session:
            info = await read_postimap_info(session)
            if info is None or not supports_folder_crud(info):
                raise HTTPException(
                    status_code=501,
                    detail=(
                        "Per-folder real-time sync requires PostIMAP "
                        f"service_version >= 1.3.0; the running instance reports "
                        f"{info.service_version if info else 'unknown'}."
                    ),
                )
            await set_folder_idle(session, folder_id, requested=real_time)

    if values:
        prefs_repo = get_folder_prefs_repo()
        await prefs_repo.update(folder_id, **values)

    async with db.session() as session:
        response = await _fetch_folder_response(session, folder_id)

    if response is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    return response
