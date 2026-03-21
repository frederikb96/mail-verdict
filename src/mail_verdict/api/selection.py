"""
Selection API for multi-select operations.

Backend-driven selection state compatible with virtual scrolling.
SelectionManager is in-memory per-account (like SyncTracker).

POST /accounts/:id/selection/toggle — toggle single mail
POST /accounts/:id/selection/range — shift-select range
POST /accounts/:id/selection/all — select all in folder
POST /accounts/:id/selection/clear — clear selection
GET /accounts/:id/selection — current selection state
POST /accounts/:id/selection/action — bulk action on selected mails
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from mail_verdict.api.deps import get_folder_repo
from mail_verdict.api.schemas import (
    BulkActionRequest,
    BulkActionResponse,
    SelectionAll,
    SelectionRange,
    SelectionResponse,
    SelectionToggle,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Mail

logger = logging.getLogger(__name__)


class SelectionManager:
    """Per-account mail selection state for virtual scroll compatibility."""

    def __init__(self, account_id: uuid.UUID) -> None:
        """
        Initialize selection state for an account.

        Args:
            account_id: Account this manager tracks
        """
        self.account_id = account_id
        self._selected: set[uuid.UUID] = set()
        self._last_toggled: uuid.UUID | None = None

    def toggle(self, mail_id: uuid.UUID) -> set[uuid.UUID]:
        """
        Toggle single mail selection.

        Args:
            mail_id: Mail to toggle

        Returns:
            Current selection set after toggle
        """
        if mail_id in self._selected:
            self._selected.discard(mail_id)
        else:
            self._selected.add(mail_id)
        self._last_toggled = mail_id
        return self._selected

    async def range_select(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        folder_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """
        Select all mails between from_id and to_id in sort order.

        Queries DB for all mail IDs between the two anchors
        in received_at DESC order (matching the list sort).

        Args:
            from_id: First anchor mail ID
            to_id: Second anchor mail ID
            folder_id: Folder to constrain range within

        Returns:
            Current selection set after range select
        """
        db = get_db_connection()
        async with db.session() as session:
            anchor_stmt = select(Mail.id, Mail.received_at).where(
                Mail.id.in_([from_id, to_id]),
                Mail.folder_id == folder_id,
            )
            result = await session.execute(anchor_stmt)
            anchors = {row.id: row.received_at for row in result}

            if from_id not in anchors or to_id not in anchors:
                raise ValueError("One or both anchor mails not found in folder")

            t1, t2 = anchors[from_id], anchors[to_id]
            lo, hi = min(t1, t2), max(t1, t2)

            range_stmt = select(Mail.id).where(
                Mail.folder_id == folder_id,
                Mail.received_at >= lo,
                Mail.received_at <= hi,
                Mail.is_deleted.is_(False),
            )
            result = await session.execute(range_stmt)
            range_ids = {row.id for row in result}

        self._selected.update(range_ids)
        return self._selected

    async def select_all(self, folder_id: uuid.UUID) -> set[uuid.UUID]:
        """
        Select all mails in a folder.

        Args:
            folder_id: Folder to select all mails from

        Returns:
            Current selection set (replaced with all folder mails)
        """
        db = get_db_connection()
        async with db.session() as session:
            stmt = select(Mail.id).where(
                Mail.folder_id == folder_id,
                Mail.is_deleted.is_(False),
            )
            result = await session.execute(stmt)
            self._selected = {row.id for row in result}
        return self._selected

    def clear(self) -> set[uuid.UUID]:
        """
        Clear all selections.

        Returns:
            Empty set
        """
        self._selected.clear()
        self._last_toggled = None
        return self._selected

    @property
    def selected_ids(self) -> set[uuid.UUID]:
        """Current set of selected mail IDs."""
        return self._selected

    @property
    def count(self) -> int:
        """Number of currently selected mails."""
        return len(self._selected)


# Global selection managers (in-memory, like SyncTracker)
_selection_managers: dict[uuid.UUID, SelectionManager] = {}


def get_selection_manager(account_id: uuid.UUID) -> SelectionManager:
    """
    Get or create a SelectionManager for an account.

    Args:
        account_id: Account UUID

    Returns:
        SelectionManager for the account
    """
    if account_id not in _selection_managers:
        _selection_managers[account_id] = SelectionManager(account_id)
    return _selection_managers[account_id]


def _make_response(manager: SelectionManager) -> SelectionResponse:
    """
    Build a SelectionResponse from current manager state.

    Args:
        manager: SelectionManager to read state from

    Returns:
        SelectionResponse with current IDs and count
    """
    return SelectionResponse(
        selected_ids=list(manager.selected_ids),
        count=manager.count,
    )


router = APIRouter(prefix="/accounts/{account_id}/selection", tags=["selection"])


@router.get("", response_model=SelectionResponse)
async def get_selection(account_id: uuid.UUID) -> SelectionResponse:
    """Get current selection state for an account."""
    manager = get_selection_manager(account_id)
    return _make_response(manager)


@router.post("/toggle", response_model=SelectionResponse)
async def toggle_selection(
    account_id: uuid.UUID,
    body: SelectionToggle,
) -> SelectionResponse:
    """Toggle selection of a single mail."""
    manager = get_selection_manager(account_id)
    manager.toggle(body.mail_id)
    _push_selection_event(account_id, manager)
    return _make_response(manager)


@router.post("/range", response_model=SelectionResponse)
async def range_selection(
    account_id: uuid.UUID,
    body: SelectionRange,
) -> SelectionResponse:
    """Select all mails between two anchor points (shift-click)."""
    manager = get_selection_manager(account_id)
    try:
        await manager.range_select(body.from_id, body.to_id, body.folder_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _push_selection_event(account_id, manager)
    return _make_response(manager)


@router.post("/all", response_model=SelectionResponse)
async def select_all(
    account_id: uuid.UUID,
    body: SelectionAll,
) -> SelectionResponse:
    """Select all mails in a folder."""
    manager = get_selection_manager(account_id)
    await manager.select_all(body.folder_id)
    _push_selection_event(account_id, manager)
    return _make_response(manager)


@router.post("/clear", response_model=SelectionResponse)
async def clear_selection(account_id: uuid.UUID) -> SelectionResponse:
    """Clear all selections for an account."""
    manager = get_selection_manager(account_id)
    manager.clear()
    _push_selection_event(account_id, manager)
    return _make_response(manager)


@router.post("/action", response_model=BulkActionResponse)
async def bulk_action(
    account_id: uuid.UUID,
    body: BulkActionRequest,
) -> BulkActionResponse:
    """
    Execute an action on all selected mails.

    Clears selection after successful action.
    """
    manager = get_selection_manager(account_id)
    selected = list(manager.selected_ids)

    if not selected:
        raise HTTPException(status_code=400, detail="No mails selected")

    db = get_db_connection()
    errors: list[str] = []
    affected = 0

    if body.action == "mark_read":
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(is_read=True)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    elif body.action == "mark_unread":
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(is_read=False)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    elif body.action == "star":
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(is_flagged=True)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    elif body.action == "unstar":
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(is_flagged=False)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    elif body.action == "delete":
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(is_deleted=True)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    elif body.action in ("move", "archive", "spam"):
        target_folder_id = body.target_folder_id
        if body.action in ("archive", "spam"):
            target_folder_id = await _resolve_special_folder(
                account_id, "archive" if body.action == "archive" else "spam",
            )
        if target_folder_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Target folder required for {body.action} action",
            )
        async with db.session() as session:
            result = await session.execute(
                update(Mail)
                .where(Mail.id.in_(selected), Mail.account_id == account_id)
                .values(folder_id=target_folder_id)
            )
            affected = result.rowcount  # type: ignore[attr-defined]

    else:
        raise HTTPException(status_code=400, detail=f"Unknown bulk action: {body.action}")

    # Clear selection after bulk action
    manager.clear()
    _push_selection_event(account_id, manager)

    return BulkActionResponse(
        success=len(errors) == 0,
        action=body.action,
        affected_count=affected,
        errors=errors,
    )


async def _resolve_special_folder(
    account_id: uuid.UUID,
    role: str,
) -> uuid.UUID | None:
    """
    Resolve a special folder (archive/spam) from the account's folder_mapping.

    Args:
        account_id: Account to look up
        role: Folder role key in folder_mapping (e.g., "archive", "spam")

    Returns:
        Folder UUID or None if not mapped
    """
    from mail_verdict.database.models import Account

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Account.folder_mapping).where(Account.id == account_id)
        )
        mapping = result.scalar_one_or_none()

    if not mapping or role not in mapping:
        return None

    folder_id_str = mapping[role]
    if not folder_id_str:
        return None

    try:
        return uuid.UUID(folder_id_str)
    except (ValueError, TypeError):
        # folder_mapping might store imap_name instead of UUID — resolve it
        folder_repo = get_folder_repo()
        folders = await folder_repo.get_by_account(account_id)
        target = next((f for f in folders if f.imap_name == folder_id_str), None)
        return target.id if target else None


def _push_selection_event(account_id: uuid.UUID, manager: SelectionManager) -> None:
    """
    Push a selection.changed SSE event into the EventRing.

    Args:
        account_id: Account the selection belongs to
        manager: SelectionManager with current state
    """
    from mail_verdict.api.events import push_selection_event

    push_selection_event(account_id, manager.selected_ids, manager.count)
