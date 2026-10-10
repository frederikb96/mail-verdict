"""
The glacier as a folder (design section 4.2): listed on its own, folded
into an account-wide or unified-view list alongside live mail, and
grouped into conversations the same way either half would be alone.

Glacier rows here are inserted directly rather than walked through
glacier/operations.py's copy/verify/expunge sequence -- that sequence
has its own tests (test_glacier_operations_pg.py); this file is about
what a listing query does with a *visible* glacier row already in
place, which a raw insert produces in one statement instead of the
full pipeline's several.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import list_message_page
from mail_verdict.api.schemas import MessageListResponse, UnifiedViewCreate
from mail_verdict.api.unified import (
    create_unified_view,
    list_unified_folders,
    member_folder_ids,
    set_folder_views,
)
from mail_verdict.database.connection import DatabaseConnection

# list_messages/list_unified_messages are FastAPI route functions whose
# non-Annotated parameters default to `= Query(...)` -- calling one
# directly, the way a pg test does, hands every unspecified one of those
# the literal Query descriptor object rather than its real default (see
# api/unified.py's own comment on the same trap for list_unified_messages).
# list_message_page is the underlying function both routes call once
# their own querystring parsing is done, with ordinary Python defaults,
# so it is what a test calls directly.


async def _list(
    db: DatabaseConnection,
    *,
    account_id: uuid.UUID | None = None,
    folder_id: uuid.UUID | None = None,
    folder_scope: object | None = None,
    threaded: bool = False,
    since: datetime | None = None,
    before: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    around: uuid.UUID | None = None,
    limit: int = 50,
) -> MessageListResponse:
    async with db.session() as session:
        return await list_message_page(
            session, account_id=account_id, folder_id=folder_id, folder_scope=folder_scope,
            threaded=threaded, is_seen=None, since=since, before=before, after=after,
            around=around, limit=limit,
        )


_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _seed_account(session: AsyncSession, *, is_active: bool = False) -> uuid.UUID:
    # Inactive by default -- PostIMAP leaves such an account entirely
    # alone (the pg layer runs a live PostIMAP), which
    # every read-only listing test here can rely on. A unified view's
    # own membership queries filter Account.is_active themselves
    # (api/unified.py), so the one test asserting a glacier's presence
    # in a view needs is_active=True regardless of what PostIMAP then
    # does with a fake host in the background.
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), :is_active)"
        ),
        {"id": account_id, "name": f"acct-{account_id}", "is_active": is_active},
    )
    return account_id


async def _enable_glacier(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return glacier_folder_id


async def _seed_folder(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'INBOX', NULL, true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    return folder_id


async def _seed_live_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    subject: str,
    received_at: datetime,
    thread_id: uuid.UUID | None = None,
    is_seen: bool = False,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, raw_source, size_bytes, received_at, is_seen) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, :subject, "
            " 'sender@example.com', :raw_source, :size_bytes, :received_at, :is_seen)"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": thread_id or message_id, "message_id_hdr": f"<{message_id}@example.com>",
            "subject": subject, "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            "received_at": received_at, "is_seen": is_seen,
        },
    )
    return message_id


async def _seed_glacier_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    glacier_folder_id: uuid.UUID,
    subject: str,
    received_at: datetime,
    thread_id: uuid.UUID | None = None,
    is_seen: bool = False,
) -> uuid.UUID:
    """A visible ('glaciered', visible_at set) glacier row, inserted
    directly -- see this module's own docstring for why."""
    glacier_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO glacier_messages "
            "(id, account_id, folder_id, thread_id, message_id, subject, from_addr, "
            " raw_source, size_bytes, received_at, is_seen, msg_key, state, visible_at, "
            " glaciered_at) "
            "VALUES (:id, :account_id, :folder_id, :thread_id, :message_id_hdr, :subject, "
            " 'sender@example.com', :raw_source, :size_bytes, :received_at, :is_seen, "
            " :msg_key, 'glaciered', now(), now())"
        ),
        {
            "id": glacier_id, "account_id": account_id, "folder_id": glacier_folder_id,
            "thread_id": thread_id or glacier_id, "message_id_hdr": f"<{glacier_id}@example.com>",
            "subject": subject, "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
            "received_at": received_at, "is_seen": is_seen, "msg_key": f"msg-{glacier_id}",
        },
    )
    return glacier_id


@pytest.mark.asyncio
async def test_the_glacier_folder_lists_its_own_mail_alone(
    migrated_db: DatabaseConnection,
) -> None:
    """Scoped to exactly the glacier folder id, the list is glacier_messages
    alone -- no live message leaks in, no live-table union is even built."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Live", received_at=now,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Archived", received_at=now - timedelta(days=400),
        )

    response = await _list(migrated_db, account_id=account_id, folder_id=glacier_folder_id)
    assert [m.id for m in response.messages] == [glacier_id]
    assert response.messages[0].subject == "Archived"
    assert response.messages[0].pending_sync is False


@pytest.mark.asyncio
async def test_an_account_wide_list_unions_live_and_glacier_mail(
    migrated_db: DatabaseConnection,
) -> None:
    """No folder_id at all -- the widest scope -- includes this account's
    own glacier alongside every real folder, newest first."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        live_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Newest live", received_at=now,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Older glaciered", received_at=now - timedelta(days=1),
        )

    response = await _list(migrated_db, account_id=account_id, folder_id=None)
    assert [m.id for m in response.messages] == [live_id, glacier_id]


@pytest.mark.asyncio
async def test_a_real_folder_alone_never_touches_the_glacier(
    migrated_db: DatabaseConnection,
) -> None:
    """The untouched path: an installation (or a request) that cannot
    reach a glacier pays no union at all -- proven behaviourally, by a
    glaciered message from the SAME account never appearing in a plain
    real-folder list."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        live_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Live only", received_at=now,
        )
        await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Archived", received_at=now - timedelta(days=1),
        )

    response = await _list(migrated_db, account_id=account_id, folder_id=live_folder_id)
    assert [m.id for m in response.messages] == [live_id]


@pytest.mark.asyncio
async def test_threaded_listing_groups_a_conversation_across_both_tables(
    migrated_db: DatabaseConnection,
) -> None:
    """A conversation with its older half glaciered and its newer half
    still live is one thread row, its own thread_id unchanged by
    glaciering (design section 3.9) -- counted from both tables."""
    now = datetime.now(timezone.utc)
    thread_id = uuid.uuid4()
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        newest_id = await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Re: Thread", received_at=now, thread_id=thread_id,
        )
        await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Thread", received_at=now - timedelta(days=2), thread_id=thread_id,
            is_seen=True,
        )

    response = await _list(migrated_db, account_id=account_id, folder_id=None, threaded=True)
    assert len(response.messages) == 1
    row = response.messages[0]
    assert row.id == newest_id
    assert row.thread_count == 2
    assert row.unread_in_thread == 1


@pytest.mark.asyncio
async def test_around_resolves_a_glacier_id_in_a_union_scope(
    migrated_db: DatabaseConnection,
) -> None:
    """Centring a fresh page on a glacier id, listing account-wide, must
    resolve it rather than 404 -- the cursor/around lookups have to
    reach the same entity the ordinary page does."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        glacier_folder_id = await _enable_glacier(session, account_id)
        live_folder_id = await _seed_folder(session, account_id)
        await _seed_live_message(
            session, account_id=account_id, folder_id=live_folder_id,
            subject="Newer", received_at=now,
        )
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="Older", received_at=now - timedelta(days=1),
        )

    response = await _list(
        migrated_db, account_id=account_id, folder_id=None, around=glacier_id, limit=10,
    )
    assert glacier_id in [m.id for m in response.messages]


@pytest.mark.asyncio
async def test_a_unified_view_including_the_glacier_lists_it(
    migrated_db: DatabaseConnection,
) -> None:
    """set_folder_views accepts a glacier id, member_folder_ids resolves
    it, and the view's own message list includes what it holds --
    design section 4.5."""
    now = datetime.now(timezone.utc)
    async with migrated_db.session() as session:
        account_id = await _seed_account(session, is_active=True)
        glacier_folder_id = await _enable_glacier(session, account_id)
        glacier_id = await _seed_glacier_message(
            session, account_id=account_id, glacier_folder_id=glacier_folder_id,
            subject="In the view", received_at=now,
        )

    view = await create_unified_view(UnifiedViewCreate(name="Everything Archived"))
    async with migrated_db.session() as session:
        await set_folder_views(session, glacier_folder_id, [view.id])

    folders = await list_unified_folders()
    this_view = next(v for v in folders if v.id == view.id)
    assert any(f.folder_id == glacier_folder_id for f in this_view.folders)
    assert this_view.total_count == 1

    messages = await _list(migrated_db, folder_scope=member_folder_ids(view.id))
    assert [m.id for m in messages.messages] == [glacier_id]
