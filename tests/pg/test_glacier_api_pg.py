"""
The glacier through the API surface: enabling it on an account, moving a
message into it via the ordinary message action, reading it back, and
the guards that keep it from being disabled or deleted while it still
holds the only copy of something.

Moving a message in is refused outright unless the running PostIMAP
carries outbox kind="append" (tests/pg/test_glacier_gate_pg.py proves
the refusal itself), so every test below that needs a message actually
inside the glacier skips itself, with the running version named in the
reason, against the pinned default image -- exactly the exception
tests/e2e/test_glacier_restore_flow.py already documents for the same
capability. Point MAIL_VERDICT_TEST_POSTIMAP_IMAGE (see
tests/setup/images.py) at a capable build to run this file for real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.accounts import delete_account, list_folders, update_account
from mail_verdict.api.mails import (
    get_attachment,
    get_message,
    get_message_quote,
    get_raw_source,
    locate_message,
)
from mail_verdict.api.mails import message_action as api_message_action
from mail_verdict.api.schemas import AccountUpdateRequest, MessageActionRequest
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import confirm_or_withdraw_removing
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _seed_ready_message(
    db: DatabaseConnection, *, glacier_enabled: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account (glacier enabled unless told otherwise), one archive
    folder, one eligible message. Returns (account_id, folder_id,
    message_id)."""
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts "
                "(id, name, imap_host, imap_port, imap_user, imap_password) "
                "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'))"
            ),
            {"id": account_id, "name": f"acct-{account_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO folders (id, account_id, imap_name, special_use, "
                "initial_sync_done) VALUES (:id, :account_id, 'Archive', 'archive', true)"
            ),
            {"id": folder_id, "account_id": account_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, "
                " 'Test', 'sender@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": message_id, "account_id": account_id, "folder_id": folder_id,
                "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
                "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )
        if glacier_enabled:
            await session.execute(
                text(
                    "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                    "VALUES (:account_id, true, :glacier_folder_id)"
                ),
                {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
            )
    return account_id, folder_id, message_id


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so moving a message into the glacier is correctly refused rather than "
            "exercised here; see this module's own docstring"
        )


async def _glacier_folder_id(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    return (
        await session.execute(
            text("SELECT glacier_folder_id FROM account_prefs WHERE account_id = :id"),
            {"id": account_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_enabling_glacier_assigns_a_folder_id_once(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, _, _ = await _seed_ready_message(migrated_db, glacier_enabled=False)

    response = await update_account(
        account_id, AccountUpdateRequest(glacier_enabled=True),
    )
    assert response.glacier_enabled is True
    first_id = response.glacier_folder_id
    assert first_id is not None

    # Toggling it off then on again keeps the same id (D6/D7).
    response = await update_account(account_id, AccountUpdateRequest(glacier_enabled=False))
    assert response.glacier_enabled is False
    assert response.glacier_folder_id == first_id
    response = await update_account(account_id, AccountUpdateRequest(glacier_enabled=True))
    assert response.glacier_folder_id == first_id


@pytest.mark.asyncio
async def test_folder_listing_shows_the_glacier_only_when_enabled(
    migrated_db: DatabaseConnection,
) -> None:
    account_id, _, _ = await _seed_ready_message(migrated_db, glacier_enabled=False)
    folders = await list_folders(account_id)
    assert not any(f.kind == "glacier" for f in folders)

    await update_account(account_id, AccountUpdateRequest(glacier_enabled=True))
    folders = await list_folders(account_id)
    glacier_folders = [f for f in folders if f.kind == "glacier"]
    assert len(glacier_folders) == 1
    assert glacier_folders[0].imap_name == "Glacier"
    assert glacier_folders[0].total_count == 0


@pytest.mark.asyncio
async def test_move_action_into_glacier_and_read_it_back(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)

    response = await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    assert response.success is True

    async with migrated_db.session() as session:
        live = (
            await session.execute(
                text("SELECT expunged_at FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).mappings().one()
        assert live["expunged_at"] is not None
        glacier_row = (
            await session.execute(
                text("SELECT id, state FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).mappings().one()
        glacier_id = glacier_row["id"]

    # The response is a caller's only synchronous way to learn the new
    # id -- there is no live row left to look it up from afterward.
    assert response.message_id == glacier_id
    assert response.folder_id == glacier_folder_id

    location = await locate_message(glacier_id)
    assert location.account_id == account_id
    assert location.folder_id == glacier_folder_id

    detail = await get_message(glacier_id)
    assert detail.is_glacier is True
    assert detail.origin_folder_name == "Archive"
    assert detail.subject == "Test"

    raw_response = await get_raw_source(glacier_id)
    assert raw_response.body == _RAW_SOURCE

    # The reply/forward compose flow's only server call besides the
    # already-fetched message detail: quoting a glaciered message must
    # not 404, or replying to one is silently broken. (No body_text/html
    # was seeded, so an empty quote is the correct answer here -- the
    # point is that this call succeeds at all.)
    quote = await get_message_quote(glacier_id)
    assert quote.html == ""


@pytest.mark.asyncio
async def test_attachment_survives_the_move_and_downloads_byte_identical(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    attachment_id = uuid.uuid4()
    attachment_data = b"some pdf bytes"
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO attachments (id, message_id, filename, content_type, size_bytes, "
                "data) VALUES (:id, :message_id, 'f.pdf', 'application/pdf', :size, :data)"
            ),
            {
                "id": attachment_id, "message_id": message_id, "size": len(attachment_data),
                "data": attachment_data,
            },
        )
        glacier_folder_id = await _glacier_folder_id(session, account_id)

    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    async with migrated_db.session() as session:
        glacier_id = (
            await session.execute(
                text("SELECT id FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).scalar_one()
        glacier_attachment_id = (
            await session.execute(
                text(
                    "SELECT id FROM glacier_attachments WHERE glacier_message_id = :id"
                ),
                {"id": glacier_id},
            )
        ).scalar_one()

    response = await get_attachment(glacier_id, glacier_attachment_id)
    assert response.body == attachment_data


@pytest.mark.asyncio
async def test_expunge_on_a_glaciered_message_requires_confirmation(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    async with migrated_db.session() as session:
        glacier_id = (
            await session.execute(
                text("SELECT id FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).scalar_one()

    unconfirmed = await api_message_action(glacier_id, MessageActionRequest(action="expunge"))
    assert unconfirmed.success is False
    async with migrated_db.session() as session:
        still_there = (
            await session.execute(
                text("SELECT 1 FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).scalar_one_or_none()
        assert still_there is not None

    confirmed = await api_message_action(
        glacier_id, MessageActionRequest(action="expunge", confirm=True),
    )
    assert confirmed.success is True
    async with migrated_db.session() as session:
        gone = (
            await session.execute(
                text("SELECT 1 FROM glacier_messages WHERE id = :id"), {"id": glacier_id},
            )
        ).scalar_one_or_none()
        assert gone is None


@pytest.mark.asyncio
async def test_disabling_glacier_while_it_holds_mail_is_refused(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_account(account_id, AccountUpdateRequest(glacier_enabled=False))
    assert exc_info.value.status_code == 409
    assert "1 message" in exc_info.value.detail

    with pytest.raises(HTTPException) as exc_info:
        await delete_account(account_id)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_restoring_through_the_move_action(migrated_db: DatabaseConnection) -> None:
    """The refusal when the running PostIMAP lacks the capability is
    tests/pg/test_glacier_gate_pg.py's own subject, exhaustively; this
    test is the allow path, so it needs a capable build to run for real
    rather than trivially pass on a refusal it never asked for -- see
    this module's own docstring."""
    await _skip_unless_append_capable(migrated_db)
    account_id, folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    async with migrated_db.session() as session:
        glacier_id = (
            await session.execute(
                text("SELECT id FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).scalar_one()

    # Restore only works once the message has actually left "removing"
    # for "glaciered" -- confirm_or_withdraw_removing is what promotes
    # it, the same bookkeeping step the automatic sweep runs on its own
    # tick. grace_seconds=0 skips the ordinary wait, the same shape the
    # pg operations tests already use for this.
    confirmed, withdrawn = await confirm_or_withdraw_removing(
        migrated_db, account_id, grace_seconds=0,
    )
    assert (confirmed, withdrawn) == (1, 0)

    response = await api_message_action(
        glacier_id, MessageActionRequest(action="move", target_folder_id=folder_id),
    )
    assert response.success is True, response.message

    async with migrated_db.session() as session:
        restoring = (
            await session.execute(
                text(
                    "SELECT state, restore_outbox_id FROM glacier_messages WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert restoring["state"] == "restoring"
        assert restoring["restore_outbox_id"] is not None


async def _seed_and_glacier(migrated_db: DatabaseConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """Move the message _seed_ready_message creates into the glacier and
    confirm it all the way to 'glaciered'. Returns (account_id,
    glacier_id)."""
    account_id, _folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    async with migrated_db.session() as session:
        glacier_id = (
            await session.execute(
                text("SELECT id FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).scalar_one()
    confirmed, withdrawn = await confirm_or_withdraw_removing(
        migrated_db, account_id, grace_seconds=0,
    )
    assert (confirmed, withdrawn) == (1, 0)
    return account_id, glacier_id


@pytest.mark.asyncio
async def test_spam_refusal_on_a_glacier_row_answers_applied_false(
    migrated_db: DatabaseConnection,
) -> None:
    """Spam rulings on a glaciered message are refused outright -- a
    client keying on `applied` must not read the refusal as having
    worked."""
    await _skip_unless_append_capable(migrated_db)
    _account_id, glacier_id = await _seed_and_glacier(migrated_db)

    response = await api_message_action(glacier_id, MessageActionRequest(action="spam"))
    assert response.success is False
    assert response.applied is False


@pytest.mark.asyncio
async def test_duplicate_identity_refusal_answers_applied_false(
    migrated_db: DatabaseConnection,
) -> None:
    """A different message forged under the same Message-ID as an
    already-glaciered row is refused -- the server-side guard already
    keeps the live message intact (test_glacier_operations_pg.py's own
    coverage); this is the API-level honesty of that refusal."""
    await _skip_unless_append_capable(migrated_db)
    account_id, _folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
        message_id_hdr = (
            await session.execute(
                text("SELECT message_id FROM messages WHERE id = :id"), {"id": message_id},
            )
        ).scalar_one()
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    await confirm_or_withdraw_removing(migrated_db, account_id, grace_seconds=0)

    async with migrated_db.session() as session:
        archive_folder_id = (
            await session.execute(
                text(
                    "SELECT id FROM folders WHERE account_id = :account_id "
                    "AND special_use = 'archive'"
                ),
                {"account_id": account_id},
            )
        ).scalar_one()
        forged_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
                " from_addr, raw_source, size_bytes, received_at) "
                "VALUES (:id, :account_id, :folder_id, 2, :thread_id, :message_id_hdr, "
                " 'Forged', 'attacker@example.com', :raw_source, :size_bytes, :received_at)"
            ),
            {
                "id": forged_id, "account_id": account_id, "folder_id": archive_folder_id,
                "thread_id": forged_id, "message_id_hdr": message_id_hdr,
                "raw_source": b"different content entirely",
                "size_bytes": len(b"different content entirely"),
                "received_at": datetime.now(timezone.utc) - timedelta(days=400),
            },
        )

    response = await api_message_action(
        forged_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    assert response.success is False
    assert response.applied is False


@pytest.mark.asyncio
async def test_mark_keyword_and_restore_answers_name_the_glacier_folder(
    migrated_db: DatabaseConnection,
) -> None:
    """The origin folder id -- stale the instant a message leaves it, and
    read straight off the pre-glacier live row by the generic re-read
    every other action reuses -- must never leak into an answer about a
    glacier row."""
    await _skip_unless_append_capable(migrated_db)
    account_id, glacier_id = await _seed_and_glacier(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
        archive_folder_id = (
            await session.execute(
                text(
                    "SELECT id FROM folders WHERE account_id = :account_id "
                    "AND special_use = 'archive'"
                ),
                {"account_id": account_id},
            )
        ).scalar_one()

    mark_response = await api_message_action(
        glacier_id, MessageActionRequest(action="mark_read"),
    )
    assert mark_response.folder_id == glacier_folder_id
    assert mark_response.folder_id != archive_folder_id

    keyword_response = await api_message_action(
        glacier_id, MessageActionRequest(action="keyword_add", keyword="Important"),
    )
    assert keyword_response.folder_id == glacier_folder_id

    restore_response = await api_message_action(
        glacier_id, MessageActionRequest(action="move", target_folder_id=archive_folder_id),
    )
    assert restore_response.success is True, restore_response.message
    # Still in the glacier at the moment of this response -- the restore
    # is an async outbox append, only confirmed later.
    assert restore_response.folder_id == glacier_folder_id


@pytest.mark.asyncio
async def test_restore_not_ready_message_names_the_remaining_seconds(
    migrated_db: DatabaseConnection,
) -> None:
    """A row still mid-flight (state='removing', not yet 'glaciered')
    gets a reason naming roughly how much longer that will take, rather
    than the same opaque string every other not-ready case shares."""
    await _skip_unless_append_capable(migrated_db)
    account_id, _folder_id, message_id = await _seed_ready_message(migrated_db)
    async with migrated_db.session() as session:
        glacier_folder_id = await _glacier_folder_id(session, account_id)
        archive_folder_id = (
            await session.execute(
                text(
                    "SELECT id FROM folders WHERE account_id = :account_id "
                    "AND special_use = 'archive'"
                ),
                {"account_id": account_id},
            )
        ).scalar_one()
    await api_message_action(
        message_id, MessageActionRequest(action="move", target_folder_id=glacier_folder_id),
    )
    async with migrated_db.session() as session:
        glacier_id = (
            await session.execute(
                text("SELECT id, state FROM glacier_messages WHERE origin_message_id = :id"),
                {"id": message_id},
            )
        ).mappings().one()
        assert glacier_id["state"] == "removing"
        glacier_id = glacier_id["id"]
    # Deliberately not confirmed -- still sitting in "removing".

    response = await api_message_action(
        glacier_id, MessageActionRequest(action="move", target_folder_id=archive_folder_id),
    )
    assert response.success is False
    assert response.message is not None
    assert "second" in response.message
    assert any(char.isdigit() for char in response.message)
