"""
The glacier's full round trip against a real Dovecot: a message is
glaciered (genuinely gone from the IMAP server, checked over IMAP, not
only in the mirror), then restored (genuinely back on the server, byte
for byte, checked the same way) -- design section 13.2's own production
trial, run here against the test stack instead.

Needs a PostIMAP build carrying outbox kind="append"
(service_version >= glacier's own MIN_MESSAGE_APPEND_SERVICE_VERSION).
Skips itself with a clear reason against an older one, rather than
either failing on every ordinary run or silently reporting green for a
path that never ran -- the reason is asserted directly into the skip
message, so a run against the wrong PostIMAP is never mistaken for a
pass, only for a documented skip.
"""

from __future__ import annotations

import re
import uuid
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from typing import Any

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.glacier.operations import confirm_or_withdraw_removing, glacier_message_now
from mail_verdict.glacier.restore import confirm_restores, start_restore
from mail_verdict.postimap.contract import read_postimap_info, supports_message_append
from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_account_active,
    wait_for_async,
    wait_for_folder,
)
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.imap_helpers import find_message_by_id, imap_session, wait_for_flags
from tests.setup.mail_delivery import build_eml, deliver_message

_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]*)"')
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


@pytest.mark.asyncio
async def test_glacier_round_trip_against_real_dovecot(
    app_client: TestClient,
    dovecot_endpoint: tuple[str, int, int],
    db: DatabaseConnection,
) -> None:
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "the glacier restore round trip cannot run against it"
        )

    host, imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("glacier-restore")
    msg_id = f"<glacier-restore-{uuid.uuid4()}@example.com>"
    original_bytes = build_eml(
        sender="sender@example.com", recipient=email, subject="Glacier round trip",
        body="This message goes to the glacier and comes back.",
        message_id=msg_id,
    )
    deliver_message(original_bytes, host, lmtp_port, sender="sender@example.com", recipient=email)

    resp = app_client.post(
        "/api/accounts",
        json={
            "name": email, "imap_host": DOVECOT_ALIAS, "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email, "imap_password": DOVECOT_PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    account_id = resp.json()["id"]
    wait_for_account_active(app_client, account_id)
    inbox = wait_for_folder(app_client, account_id, "INBOX")

    def _find_message() -> dict[str, Any] | None:
        listing = app_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": inbox["id"]},
        )
        assert listing.status_code == 200, listing.text
        for row in listing.json()["messages"]:
            if row["subject"] == "Glacier round trip":
                return row
        return None

    live_row = wait_for(_find_message, description="the delivered message to sync into the mirror")
    message_id = live_row["id"]

    # The server's own copy, not the bytes handed to LMTP: delivery adds trace
    # headers (Return-Path, Delivered-To, Received, ...) the same way any real
    # MTA does, so this -- not original_bytes -- is the byte-for-byte baseline
    # every later fetch in this test is compared against.
    with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
        seq = find_message_by_id(conn, "INBOX", msg_id)
        assert seq is not None, (
            "the message must actually be on the server before this test can prove "
            "the glacier removes it"
        )
        conn.select("INBOX")
        typ, fetch_data = conn.fetch(seq.decode(), "(RFC822)")
        assert typ == "OK" and fetch_data and isinstance(fetch_data[0], tuple)
        server_bytes: bytes = fetch_data[0][1]

    # Mark it read and flagged through the app's own action -- design
    # section 7 says a restore must carry the message's original date
    # and flags, and start_restore builds the APPEND's flags/internal_date
    # from exactly is_seen/is_flagged/received_at on the glacier row, so
    # this is what proves that path for real rather than by reading the
    # code. Through the API (not a direct IMAP STORE) so the mirror --
    # what copy_message actually reads from -- picks it up the same way
    # a person starring a message would.
    action_resp = app_client.post(
        f"/api/messages/{message_id}/action", json={"action": "mark_read"},
    )
    assert action_resp.status_code == 200, action_resp.text
    action_resp = app_client.post(f"/api/messages/{message_id}/action", json={"action": "flag"})
    assert action_resp.status_code == 200, action_resp.text
    wait_for_flags(
        host, imap_port, email, DOVECOT_PASSWORD, "INBOX", msg_id, {"\\Seen", "\\Flagged"},
    )

    with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
        seq = find_message_by_id(conn, "INBOX", msg_id)
        assert seq is not None
        conn.select("INBOX")
        typ, date_data = conn.fetch(seq.decode(), "(INTERNALDATE)")
        assert typ == "OK" and date_data and isinstance(date_data[0], bytes)
        original_internaldate = date_data[0]

    # Enable the glacier on this account (account_prefs is MailVerdict's own table --
    # a plain upsert, the same shape update_account's own handler uses).
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id) "
                "ON CONFLICT (account_id) DO UPDATE SET glacier_enabled = true, "
                "glacier_folder_id = :glacier_folder_id"
            ),
            {"account_id": uuid.UUID(account_id), "glacier_folder_id": uuid.uuid4()},
        )

    outcome = await glacier_message_now(db, uuid.UUID(message_id))
    assert outcome.ok, outcome.reason
    glacier_id = outcome.glacier_id
    assert glacier_id is not None

    def _gone_from_inbox() -> bool:
        with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
            return find_message_by_id(conn, "INBOX", msg_id) is None

    wait_for(_gone_from_inbox, description="the EXPUNGE to actually reach the real IMAP server")

    # The sweep's own bookkeeping step, run directly rather than waiting for its
    # timer -- the same shape the pg-layer tests already use for this.
    confirmed, withdrawn = await confirm_or_withdraw_removing(
        db, uuid.UUID(account_id), grace_seconds=0,
    )
    assert (confirmed, withdrawn) == (1, 0)

    async with db.session() as session:
        state = (
            await session.execute(
                text("SELECT state, raw_source FROM glacier_messages WHERE id = :id"),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert state["state"] == "glaciered"
        assert state["raw_source"] == server_bytes

    restore_outcome = await start_restore(db, glacier_id, uuid.UUID(inbox["id"]))
    assert restore_outcome.ok, restore_outcome.reason

    def _back_on_the_server() -> bytes | None:
        with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
            seq = find_message_by_id(conn, "INBOX", msg_id)
            if seq is None:
                return None
            conn.select("INBOX")
            typ, data = conn.fetch(seq.decode(), "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                return None
            return data[0][1]

    restored_bytes = wait_for(
        _back_on_the_server, timeout_s=60.0,
        description="the APPEND to actually land the message back on the real IMAP server",
    )
    assert restored_bytes == server_bytes, "the restored message must be byte-identical"

    with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
        seq = find_message_by_id(conn, "INBOX", msg_id)
        assert seq is not None
        conn.select("INBOX")
        typ, restored_date_data = conn.fetch(seq.decode(), "(INTERNALDATE FLAGS)")
        assert typ == "OK" and restored_date_data and isinstance(restored_date_data[0], bytes)
        restored_line = restored_date_data[0]

    original_date_str = _INTERNALDATE_RE.search(original_internaldate)
    restored_date_str = _INTERNALDATE_RE.search(restored_line)
    assert original_date_str is not None and restored_date_str is not None
    assert restored_date_str.group(1) == original_date_str.group(1), (
        "the restored message must land with its original INTERNALDATE"
    )
    restored_flags_match = _FLAGS_RE.search(restored_line)
    assert restored_flags_match is not None
    restored_flags = {f.decode() for f in restored_flags_match.group(1).split()}
    assert {"\\Seen", "\\Flagged"} <= restored_flags, (
        "the restored message must carry its original read/flagged state"
    )

    confirmed_count = await wait_for_async(
        lambda: confirm_restores(db, uuid.UUID(account_id)),
        description="confirm_restores to see the appended copy sync back into the mirror",
    )
    assert confirmed_count == 1

    async with db.session() as session:
        tombstone = (
            await session.execute(
                text(
                    "SELECT restored_at, visible_at, raw_source FROM glacier_messages "
                    "WHERE id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
        assert tombstone["restored_at"] is not None
        assert tombstone["visible_at"] is None
        assert tombstone["raw_source"] is None

        live_again = (
            await session.execute(
                text(
                    "SELECT id, imap_uid, raw_source FROM messages "
                    "WHERE account_id = :account_id AND message_id = :message_id_hdr "
                    "AND expunged_at IS NULL"
                ),
                {"account_id": uuid.UUID(account_id), "message_id_hdr": msg_id},
            )
        ).mappings().one()
        assert live_again["imap_uid"] is not None
        assert live_again["raw_source"] == server_bytes


@pytest.mark.asyncio
async def test_glacier_round_trip_preserves_an_attachment(
    app_client: TestClient,
    dovecot_endpoint: tuple[str, int, int],
    db: DatabaseConnection,
) -> None:
    """design section 8.6's own claim ("everything of a message is
    preserved... its attachments") proven the same way as the byte-for-
    byte message body: through the real APPEND, never by only checking
    glacier_attachments before restore. Attachment survival through the
    move into the glacier is already covered at the pg layer
    (test_glacier_api_pg.py); what only a real Dovecot round trip proves
    is that the *restored* live message's own `attachments` row comes
    back too -- PostIMAP re-parses MIME from the appended raw_source the
    same way it does for any newly-synced message, so this is really
    proving that path, not anything MailVerdict itself does differently
    for an attachment specifically.
    """
    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is None or not supports_message_append(info):
        pytest.skip(
            'this PostIMAP build does not carry outbox kind="append" -- '
            f"reports service_version={info.service_version if info else 'unknown'}, "
            "the glacier restore round trip cannot run against it"
        )

    host, imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("glacier-attachment")
    msg_id = f"<glacier-attachment-{uuid.uuid4()}@example.com>"
    attachment_bytes = b"%PDF-1.4 fake pdf content for the round trip\n"

    mime_msg = EmailMessage()
    mime_msg["From"] = "sender@example.com"
    mime_msg["To"] = email
    mime_msg["Subject"] = "Glacier attachment round trip"
    mime_msg["Message-ID"] = msg_id
    mime_msg.set_content("This message carries an attachment through the glacier and back.")
    mime_msg.add_attachment(
        attachment_bytes, maintype="application", subtype="pdf", filename="report.pdf",
    )
    original_bytes = mime_msg.as_bytes(policy=SMTP_POLICY)
    deliver_message(original_bytes, host, lmtp_port, sender="sender@example.com", recipient=email)

    resp = app_client.post(
        "/api/accounts",
        json={
            "name": email, "imap_host": DOVECOT_ALIAS, "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email, "imap_password": DOVECOT_PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    account_id = resp.json()["id"]
    wait_for_account_active(app_client, account_id)
    inbox = wait_for_folder(app_client, account_id, "INBOX")

    def _find_message() -> dict[str, Any] | None:
        listing = app_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": inbox["id"]},
        )
        assert listing.status_code == 200, listing.text
        for row in listing.json()["messages"]:
            if row["subject"] == "Glacier attachment round trip":
                return row
        return None

    live_row = wait_for(_find_message, description="the delivered message to sync into the mirror")
    message_id = live_row["id"]

    def _has_attachment() -> bool:
        detail = app_client.get(f"/api/messages/{message_id}")
        assert detail.status_code == 200, detail.text
        return len(detail.json()["attachments"]) == 1

    wait_for(_has_attachment, description="PostIMAP to parse and mirror the attachment")

    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
                "VALUES (:account_id, true, :glacier_folder_id) "
                "ON CONFLICT (account_id) DO UPDATE SET glacier_enabled = true, "
                "glacier_folder_id = :glacier_folder_id"
            ),
            {"account_id": uuid.UUID(account_id), "glacier_folder_id": uuid.uuid4()},
        )

    outcome = await glacier_message_now(db, uuid.UUID(message_id))
    assert outcome.ok, outcome.reason
    glacier_id = outcome.glacier_id
    assert glacier_id is not None

    async with db.session() as session:
        glacier_attachment = (
            await session.execute(
                text(
                    "SELECT filename, data FROM glacier_attachments "
                    "WHERE glacier_message_id = :id"
                ),
                {"id": glacier_id},
            )
        ).mappings().one()
    assert glacier_attachment["filename"] == "report.pdf"
    assert glacier_attachment["data"] == attachment_bytes

    def _gone_from_inbox() -> bool:
        with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
            return find_message_by_id(conn, "INBOX", msg_id) is None

    wait_for(_gone_from_inbox, description="the EXPUNGE to actually reach the real IMAP server")

    confirmed, withdrawn = await confirm_or_withdraw_removing(
        db, uuid.UUID(account_id), grace_seconds=0,
    )
    assert (confirmed, withdrawn) == (1, 0)

    restore_outcome = await start_restore(db, glacier_id, uuid.UUID(inbox["id"]))
    assert restore_outcome.ok, restore_outcome.reason

    def _back_on_the_server() -> bool:
        with imap_session(host, imap_port, email, DOVECOT_PASSWORD) as conn:
            return find_message_by_id(conn, "INBOX", msg_id) is not None

    wait_for(
        _back_on_the_server, timeout_s=60.0,
        description="the APPEND to actually land the message back on the real IMAP server",
    )

    await wait_for_async(
        lambda: confirm_restores(db, uuid.UUID(account_id)),
        description="confirm_restores to see the appended copy sync back into the mirror",
    )

    async with db.session() as session:
        live_again = (
            await session.execute(
                text(
                    "SELECT id FROM messages WHERE account_id = :account_id "
                    "AND message_id = :message_id_hdr AND expunged_at IS NULL"
                ),
                {"account_id": uuid.UUID(account_id), "message_id_hdr": msg_id},
            )
        ).mappings().one()
        restored_message_id = str(live_again["id"])

    def _restored_attachment_synced() -> dict[str, Any] | None:
        detail = app_client.get(f"/api/messages/{restored_message_id}")
        assert detail.status_code == 200, detail.text
        attachments = detail.json()["attachments"]
        return attachments[0] if len(attachments) == 1 else None

    restored_attachment = wait_for(
        _restored_attachment_synced,
        description="PostIMAP to re-parse the restored message's MIME and mirror its attachment",
    )
    assert restored_attachment["filename"] == "report.pdf"

    download = app_client.get(
        f"/api/messages/{restored_message_id}/attachments/{restored_attachment['id']}",
    )
    assert download.status_code == 200, download.text
    assert download.content == attachment_bytes, (
        "the restored message's attachment must download byte-identical"
    )
