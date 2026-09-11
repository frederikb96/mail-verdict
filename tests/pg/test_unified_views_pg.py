"""
Unified views as their own rows: one folder in several views, a view's own
list paging and threading the same way an account's does, and a message's
current location after a move made in another mail client.

Endpoints are called directly, the way test_cursor_pagination_null_received_at
does -- every one of them reads the global connection migrated_db sets up.
Optional query parameters are always passed explicitly for the same reason:
called directly, a bare `Query(...)` default arrives as a descriptor object.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.folder_management import update_folder_prefs
from mail_verdict.api.mails import locate_message
from mail_verdict.api.schemas import (
    FolderPrefsUpdate,
    UnifiedFolderOrderUpdate,
    UnifiedViewCreate,
    UnifiedViewUpdate,
)
from mail_verdict.api.unified import (
    create_unified_view,
    delete_unified_view,
    get_unified_folder_order,
    list_unified_folders,
    list_unified_messages,
    set_unified_folder_order,
    update_unified_view,
)
from mail_verdict.database.connection import DatabaseConnection
from tests.pg.test_threaded_messages import _seed_account_and_inbox, _seed_message

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID, name: str) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, :name)"
        ),
        {"id": folder_id, "account_id": account_id, "name": name},
    )
    return folder_id


async def _activate(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE accounts SET is_active = true WHERE id = :id"), {"id": account_id},
    )


async def _assign(folder_id: uuid.UUID, *view_ids: uuid.UUID) -> list[uuid.UUID]:
    response = await update_folder_prefs(
        folder_id, FolderPrefsUpdate(unified_view_ids=list(view_ids)),
    )
    return response.unified_view_ids


async def _list_ids(
    view_name: str, *, threaded: bool = False, is_seen: bool | None = None,
    around: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    page = await list_unified_messages(
        folder_name=view_name, before=None, limit=50,
        threaded=threaded, is_seen=is_seen, after=None, around=around, since=None,
    )
    return [m.id for m in page.messages]


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_a_folder_in_two_views_lists_its_mail_in_both(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        await _activate(session, account_id)
        work_id = await _seed_folder(session, account_id, "Work")
        in_inbox = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME, is_seen=False, uid=1,
        )
        in_work = await _seed_message(
            session, account_id=account_id, folder_id=work_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=2,
        )

    everything = await create_unified_view(
        UnifiedViewCreate(name=_unique("Everything"), emoji="📥"),
    )
    work_only = await create_unified_view(UnifiedViewCreate(name=_unique("Work only")))

    assert await _assign(inbox_id, everything.id) == [everything.id]
    # Sidebar order: everything was created first, so it leads.
    assert await _assign(work_id, work_only.id, everything.id) == [everything.id, work_only.id]

    views = {v.id: v for v in await list_unified_folders()}
    assert {f.folder_id for f in views[everything.id].folders} == {inbox_id, work_id}
    assert {f.folder_id for f in views[work_only.id].folders} == {work_id}
    assert views[everything.id].emoji == "📥"
    assert (views[everything.id].total_count, views[everything.id].unread_count) == (2, 1)
    assert (views[work_only.id].total_count, views[work_only.id].unread_count) == (1, 0)

    assert set(await _list_ids(everything.name)) == {in_inbox, in_work}
    assert await _list_ids(work_only.name) == [in_work]

    # Replacing the set drops the membership left out of it, and only that one.
    assert await _assign(work_id, work_only.id) == [work_only.id]
    assert await _list_ids(everything.name) == [in_inbox]
    assert await _list_ids(work_only.name) == [in_work]

    # Deleting a view removes only the grouping.
    await _assign(work_id, work_only.id, everything.id)
    await delete_unified_view(work_only.id)
    remaining = {v.id for v in await list_unified_folders()}
    assert work_only.id not in remaining
    assert set(await _list_ids(everything.name)) == {in_inbox, in_work}
    async with migrated_db.session() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM messages WHERE id IN (:a, :b) AND expunged_at IS NULL"),
            {"a": in_inbox, "b": in_work},
        )
    assert count == 2


@pytest.mark.asyncio
async def test_an_unknown_view_is_refused_without_changing_the_membership(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        _account_id, inbox_id = await _seed_account_and_inbox(session)
    view = await create_unified_view(UnifiedViewCreate(name=_unique("Real")))
    await _assign(inbox_id, view.id)

    with pytest.raises(HTTPException) as exc:
        await _assign(inbox_id, view.id, uuid.uuid4())
    assert exc.value.status_code == 404

    async with migrated_db.session() as session:
        rows = (await session.execute(
            text("SELECT view_id FROM unified_view_folders WHERE folder_id = :f"),
            {"f": inbox_id},
        )).scalars().all()
    assert list(rows) == [view.id]


@pytest.mark.asyncio
async def test_a_unified_list_threads_a_conversation_across_two_member_folders(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        account_a, inbox_a = await _seed_account_and_inbox(session)
        account_b, inbox_b = await _seed_account_and_inbox(session)
        await _activate(session, account_a)
        await _activate(session, account_b)
        sent_a = await _seed_folder(session, account_a, "Sent")
        thread = uuid.uuid4()
        first = await _seed_message(
            session, account_id=account_a, folder_id=inbox_a, thread_id=thread,
            received_at=_BASE_TIME, is_seen=False, uid=1,
        )
        reply = await _seed_message(
            session, account_id=account_a, folder_id=sent_a, thread_id=thread,
            received_at=_BASE_TIME + timedelta(minutes=5), is_seen=True, uid=2,
        )
        other = await _seed_message(
            session, account_id=account_b, folder_id=inbox_b, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME + timedelta(minutes=1), is_seen=True, uid=1,
        )

    view = await create_unified_view(UnifiedViewCreate(name=_unique("All")))
    for folder_id in (inbox_a, sent_a, inbox_b):
        await _assign(folder_id, view.id)

    assert await _list_ids(view.name) == [reply, other, first]

    threaded = await list_unified_messages(
        folder_name=view.name, before=None, limit=50,
        threaded=True, is_seen=None, after=None, around=None, since=None,
    )
    assert [m.id for m in threaded.messages] == [reply, other]
    assert (threaded.messages[0].thread_count, threaded.messages[0].unread_in_thread) == (2, 1)

    # Unread only: the conversation is represented by its unread member.
    assert await _list_ids(view.name, is_seen=False) == [first]
    assert await _list_ids(view.name, threaded=True, is_seen=False) == [first]

    # A page centred on the older message resolves to its thread's row.
    assert await _list_ids(view.name, threaded=True, around=first) == [reply, other]

    # A folder on an inactive account drops out of the view.
    async with migrated_db.session() as session:
        await session.execute(
            text("UPDATE accounts SET is_active = false WHERE id = :id"), {"id": account_b},
        )
    assert await _list_ids(view.name) == [reply, first]


@pytest.mark.asyncio
async def test_views_are_named_uniquely_renamed_and_reordered(
    migrated_db: DatabaseConnection,
) -> None:
    first = await create_unified_view(UnifiedViewCreate(name=_unique("First")))
    second = await create_unified_view(UnifiedViewCreate(name=_unique("Second"), emoji="⭐"))

    with pytest.raises(HTTPException) as exc:
        await create_unified_view(UnifiedViewCreate(name=first.name))
    assert exc.value.status_code == 409

    renamed = await update_unified_view(first.id, UnifiedViewUpdate(name=_unique("Renamed")))
    assert renamed.name != first.name
    cleared = await update_unified_view(second.id, UnifiedViewUpdate(emoji=None))
    assert cleared.emoji is None
    with pytest.raises(HTTPException) as exc:
        await update_unified_view(second.id, UnifiedViewUpdate(name=renamed.name))
    assert exc.value.status_code == 409

    await set_unified_folder_order(UnifiedFolderOrderUpdate(order=[second.name, "no such view"]))
    order = (await get_unified_folder_order()).order
    assert order.index(second.name) < order.index(renamed.name)


@pytest.mark.asyncio
async def test_location_follows_a_move_made_in_another_mail_client(
    migrated_db: DatabaseConnection,
) -> None:
    """Another client's move is an expunge in the source plus a new row in
    the destination sharing only the Message-ID header."""
    async with migrated_db.session() as session:
        account_id, inbox_id = await _seed_account_and_inbox(session)
        work_id = await _seed_folder(session, account_id, "Work")
        original = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME, is_seen=False, uid=1,
        )
        header = f"<{original}@example.com>"
        twin = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, "
                "message_id, received_at, is_seen) VALUES (:id, :account_id, :folder_id, 7, "
                ":thread_id, :header, :received_at, false)"
            ),
            {
                "id": twin, "account_id": account_id, "folder_id": work_id,
                "thread_id": uuid.uuid4(), "header": header, "received_at": _BASE_TIME,
            },
        )
        untouched = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME, is_seen=False, uid=2,
        )
        vanished = await _seed_message(
            session, account_id=account_id, folder_id=inbox_id, thread_id=uuid.uuid4(),
            received_at=_BASE_TIME, is_seen=False, uid=3,
        )
        await session.execute(
            text("UPDATE messages SET expunged_at = now() WHERE id IN (:a, :b)"),
            {"a": original, "b": vanished},
        )

    assert (await locate_message(untouched)).folder_id == inbox_id
    moved = await locate_message(original)
    assert (moved.id, moved.folder_id) == (twin, work_id)

    with pytest.raises(HTTPException) as exc:
        await locate_message(vanished)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_overlapping_membership_writes_for_one_folder_never_collide(
    migrated_db: DatabaseConnection,
) -> None:
    """The multi-select saves on every tick, so two writes replacing one
    folder's set can overlap. Neither may fail, and the result is one of
    the two sets, never a mix."""
    async with migrated_db.session() as session:
        _account_id, inbox_id = await _seed_account_and_inbox(session)
    first = await create_unified_view(UnifiedViewCreate(name=_unique("One")))
    second = await create_unified_view(UnifiedViewCreate(name=_unique("Two")))
    await _assign(inbox_id, first.id)

    for _ in range(15):
        results = await asyncio.gather(
            _assign(inbox_id, first.id),
            _assign(inbox_id, first.id, second.id),
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, BaseException)], results
        async with migrated_db.session() as session:
            rows = set((await session.execute(
                text("SELECT view_id FROM unified_view_folders WHERE folder_id = :f"),
                {"f": inbox_id},
            )).scalars())
        assert rows in ({first.id}, {first.id, second.id}), rows
