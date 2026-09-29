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

import uuid
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
from tests.setup.imap_helpers import find_message_by_id, imap_session
from tests.setup.mail_delivery import build_eml, deliver_message


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
