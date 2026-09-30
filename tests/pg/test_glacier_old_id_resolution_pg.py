"""
A message's *original* id -- the one a client already held before it
was glaciered, from a browser tab, a saved link, a reply already
drafted against it -- must keep resolving afterward, to the glacier
copy, everywhere that id could have been used: detail, thread,
location, raw source, attachment, quote.

Run against the real glacier_message_now flow, not a hand-seeded
glacier_messages row (a UI agent's own report on this exact gap named
that as the thing that would have caught it directly rather than
needing a browser to trip over it live).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.mails import (
    get_attachment,
    get_message,
    get_message_quote,
    get_raw_source,
    locate_message,
)
from mail_verdict.api.mails import get_thread as api_get_thread
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import glacier_message_now
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append

_RAW_SOURCE = b"From: sender@example.com\r\nSubject: Test\r\n\r\nBody\r\n"


async def _skip_unless_append_capable(db: DatabaseConnection) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "so glacier_message_now is correctly refused rather than exercised here"
        )


async def _seed_ready_message(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'), true)"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO folders (id, account_id, imap_name, special_use, initial_sync_done) "
            "VALUES (:id, :account_id, 'Archive', 'archive', true)"
        ),
        {"id": folder_id, "account_id": account_id},
    )
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, subject, "
            " from_addr, body_text, raw_source, size_bytes, received_at) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id_hdr, "
            " 'Findable by its old id', 'sender@example.com', 'Body text', :raw_source, "
            " :size_bytes, now())"
        ),
        {
            "id": message_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": message_id, "message_id_hdr": f"<{message_id}@example.com>",
            "raw_source": _RAW_SOURCE, "size_bytes": len(_RAW_SOURCE),
        },
    )
    attachment_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO attachments (id, message_id, filename, content_type, size_bytes, data) "
            "VALUES (:id, :message_id, 'report.pdf', 'application/pdf', 4, :data)"
        ),
        {"id": attachment_id, "message_id": message_id, "data": b"%PDF"},
    )
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": uuid.uuid4()},
    )
    return account_id, folder_id, message_id


@pytest.mark.asyncio
async def test_get_message_resolves_the_original_id(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    detail = await get_message(message_id)
    assert detail.is_glacier is True
    assert detail.subject == "Findable by its old id"
    assert detail.id == outcome.glacier_id


@pytest.mark.asyncio
async def test_get_thread_resolves_the_original_id(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    thread = await api_get_thread(message_id)
    assert [m.id for m in thread.messages] == [outcome.glacier_id]
    assert thread.messages[0].is_glacier is True


@pytest.mark.asyncio
async def test_locate_message_resolves_the_original_id(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    location = await locate_message(message_id)
    assert location.id == outcome.glacier_id
    assert location.account_id == account_id


@pytest.mark.asyncio
async def test_get_raw_source_resolves_the_original_id(migrated_db: DatabaseConnection) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    raw = await get_raw_source(message_id)
    assert raw.body == _RAW_SOURCE


@pytest.mark.asyncio
async def test_get_message_quote_resolves_the_original_id(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason

    # No body_html was seeded, so the quote is rendered from body_text --
    # the point being tested is that this succeeds at all rather than
    # 404ing on the original id.
    quote = await get_message_quote(message_id)
    assert "Body text" in quote.html


@pytest.mark.asyncio
async def test_get_attachment_resolves_the_original_message_id(
    migrated_db: DatabaseConnection,
) -> None:
    await _skip_unless_append_capable(migrated_db)
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.commit()

    outcome = await glacier_message_now(migrated_db, message_id)
    assert outcome.ok, outcome.reason
    assert outcome.glacier_id is not None

    async with migrated_db.session() as session:
        glacier_attachment_id = (
            await session.execute(
                text(
                    "SELECT id FROM glacier_attachments WHERE glacier_message_id = :id"
                ),
                {"id": outcome.glacier_id},
            )
        ).scalar_one()

    response = await get_attachment(message_id, glacier_attachment_id)
    assert response.body == b"%PDF"


@pytest.mark.asyncio
async def test_get_message_still_reads_an_ordinary_expunge_with_no_glacier_twin(
    migrated_db: DatabaseConnection,
) -> None:
    """An id expunged by an ordinary move (another mail client moved the
    message, PostIMAP mirrors that as expunge-here plus insert-elsewhere)
    is not glaciered at all -- resolve_glacier_id finds nothing for it.
    mail_tags/verdicts/attachments are never repointed for that case
    (design section 2.5 repoints them only at glacier time), so the
    expunged row's own id still keys them correctly, and get_message
    must keep reading it rather than 404ing just because a *different*
    code path now prefers a glacier copy when one exists."""
    async with migrated_db.session() as session:
        _account_id, _folder_id, message_id = await _seed_ready_message(session)
        await session.execute(
            text("UPDATE messages SET expunged_at = now() WHERE id = :id"),
            {"id": message_id},
        )
        await session.commit()

    detail = await get_message(message_id)
    assert detail.is_glacier is False
    assert detail.id == message_id
    assert detail.subject == "Findable by its old id"
