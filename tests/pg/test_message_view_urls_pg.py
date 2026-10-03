"""
pg-layer proof that a URL past the body-excerpt cut, or hidden behind an
HTML anchor's visible text, still reaches load_message_view's output --
the actual loader wiring, not just the pure functions it calls (see
tests/unit/test_message_view_urls.py for those, and message_view.py's
module docstring for why this matters to the classify stage).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.message_view import load_message_view


async def _seed_account_and_folder(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text("INSERT INTO folders (id, account_id, imap_name) VALUES (:id, :account_id, 'INBOX')"),
        {"id": folder_id, "account_id": account_id},
    )
    return account_id, folder_id


async def _seed_message_with_body(
    session: AsyncSession, *, account_id: uuid.UUID, folder_id: uuid.UUID,
    body_text: str | None = None, body_html: str | None = None,
) -> uuid.UUID:
    mail_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, account_id, folder_id, imap_uid, thread_id, message_id, from_addr, "
            "subject, body_text, body_html, received_at, size_bytes, is_seen) "
            "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :message_id, :from_addr, "
            ":subject, :body_text, :body_html, :received_at, 1024, false)"
        ),
        {
            "id": mail_id, "account_id": account_id, "folder_id": folder_id,
            "thread_id": uuid.uuid4(), "message_id": f"<{uuid.uuid4()}@example.com>",
            "from_addr": "sender@example.com", "subject": "test",
            "body_text": body_text, "body_html": body_html,
            "received_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    )
    return mail_id


@pytest.mark.asyncio
async def test_url_past_the_truncation_cut_still_reaches_the_message_view(
    migrated_db: DatabaseConnection,
) -> None:
    """A naive prefix cut alone would drop this link entirely -- it sits
    well past _BODY_EXCERPT_CHARS."""
    long_body = ("filler word " * 1000) + "https://example.com/real-offer-link"
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        mail_id = await _seed_message_with_body(
            session, account_id=account_id, folder_id=folder_id, body_text=long_body,
        )
        await session.commit()

    async with migrated_db.session() as session:
        view = await load_message_view(session, mail_id)

    assert view is not None
    assert "https://example.com/real-offer-link" in view.body


@pytest.mark.asyncio
async def test_html_anchor_target_reaches_the_message_view_even_though_stripped_from_text(
    migrated_db: DatabaseConnection,
) -> None:
    """nh3.clean(tags=set()) discards the href along with the markup --
    the loader must recover it separately, or a "click here" phishing
    link never reaches the classify stage at all."""
    html = '<p>Please <a href="https://phish.example/verify-now">click here</a> to continue.</p>'
    async with migrated_db.session() as session:
        account_id, folder_id = await _seed_account_and_folder(session)
        mail_id = await _seed_message_with_body(
            session, account_id=account_id, folder_id=folder_id, body_html=html,
        )
        await session.commit()

    async with migrated_db.session() as session:
        view = await load_message_view(session, mail_id)

    assert view is not None
    assert "https://phish.example/verify-now" in view.body
    assert "click here" in view.body
