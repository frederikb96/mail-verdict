"""
The undo-send API surface against a real database: a send with a nonzero
undo window comes back as a PendingSendResponse rather than a real outbox
row, is listed and cancellable through GET/POST /outbox/pending, and a
zero-second window (or a draft) behaves exactly as it always did.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.outbox.pending import _process_due_sends
from mail_verdict.settings.service import init_settings_service, reset_settings_service

_OUTBOX_TARGET = "mail_verdict.api.outbox.get_db_connection"
_OUTBOX_EVENT_RING_TARGET = "mail_verdict.api.outbox.get_event_ring"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(outbox_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    """The settings service is a module-level global -- initialising it
    for this file's own undo_send_seconds override must not leak into
    whatever test runs next in the same session."""
    yield
    reset_settings_service()


async def _seed_account(session: AsyncSession) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    return account_id


async def _seed_account_and_settings(
    migrated_db: DatabaseConnection, undo_send_seconds: float,
) -> uuid.UUID:
    async with migrated_db.session() as session:
        account_id = await _seed_account(session)
        await session.commit()
    settings_service = await init_settings_service(migrated_db)
    await settings_service.update("outbox", {"undo_send_seconds": undo_send_seconds})
    return account_id


class TestUndoSendWindow:
    def test_a_send_with_a_grace_window_is_staged_not_sent(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "send_after" in body
        assert "status" not in body

    def test_the_staged_send_is_listed_as_pending(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            ).json()
            resp = client.get(f"/outbox/pending?account_id={account_id}")
        assert resp.status_code == 200, resp.text
        ids = [row["id"] for row in resp.json()]
        assert created["id"] in ids

    def test_cancelling_it_removes_it_from_the_pending_list(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            ).json()
            cancel_resp = client.post(f"/outbox/pending/{created['id']}/cancel")
            list_resp = client.get(f"/outbox/pending?account_id={account_id}")
        assert cancel_resp.status_code == 204, cancel_resp.text
        assert list_resp.json() == []

    def test_cancelling_an_unknown_id_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(f"/outbox/pending/{uuid.uuid4()}/cancel")
        assert resp.status_code == 404

    def test_cancelling_the_same_send_twice_the_second_time_is_a_404(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            ).json()
            first = client.post(f"/outbox/pending/{created['id']}/cancel")
            second = client.post(f"/outbox/pending/{created['id']}/cancel")
        assert first.status_code == 204
        assert second.status_code == 404

    def test_a_staged_send_is_followable_at_get_outbox_under_the_same_id(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """The identity returned at acceptance stays resolvable at GET
        /outbox -- the single place a caller who never learned about the
        undo-send window at all can still find and follow a send -- and
        turns into an ordinary status once the window passes, still under
        that same id."""
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, -5.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            ).json()

            staged = client.get(f"/outbox?account_id={account_id}").json()
            assert [row["id"] for row in staged] == [created["id"]]
            assert staged[0]["status"] == "pending"
            assert staged[0]["subject"] == "hi"

            client.portal.call(_process_due_sends, migrated_db)

            delivered = client.get(f"/outbox?account_id={account_id}").json()
        assert [row["id"] for row in delivered] == [created["id"]]
        assert delivered[0]["status"] == "pending"  # PostIMAP's own starting status

    def test_filtering_by_a_status_other_than_pending_excludes_a_staged_send(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
            resp = client.get(f"/outbox?account_id={account_id}&status=dead")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_a_zero_second_window_sends_immediately_as_before(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 0.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "send_after" not in body
        assert body["status"] == "pending"

    def test_a_draft_is_never_staged_however_long_the_window(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "draft",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text
        assert "send_after" not in resp.json()


class TestPendingSendCarriesReopenableContent:
    """A pending send is the one durable copy of what was written between
    pressing Send and the undo window passing -- everything a reopened
    composer needs has to survive staging, listing, and an idempotent
    replay of the same request, not just cancellation."""

    _BODY = {
        "to": ["them@example.com"],
        "cc": ["cc@example.com"],
        "subject": "hi",
        "body_text": "hi",
        "body_html": "<p>hi</p>",
        "in_reply_to": "<orig@example.com>",
        "references": ["<orig@example.com>"],
    }

    def test_staging_a_send_returns_everything_a_composer_needs(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox", json={"account_id": str(account_id), "kind": "send", **self._BODY},
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["to"] == self._BODY["to"]
        assert body["cc"] == self._BODY["cc"]
        assert body["subject"] == self._BODY["subject"]
        assert body["body_html"] == self._BODY["body_html"]
        assert body["in_reply_to"] == self._BODY["in_reply_to"]
        assert body["references"] == self._BODY["references"]
        assert body["replaces_message_id"] is None

    def test_the_pending_list_carries_the_same_fields(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox", json={"account_id": str(account_id), "kind": "send", **self._BODY},
            ).json()
            listed = client.get(f"/outbox/pending?account_id={account_id}").json()
        assert len(listed) == 1
        assert listed[0]["id"] == created["id"]
        assert listed[0]["body_html"] == self._BODY["body_html"]
        assert listed[0]["in_reply_to"] == self._BODY["in_reply_to"]
        assert listed[0]["references"] == self._BODY["references"]

    def test_an_idempotent_replay_carries_the_same_fields(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        """A repeated request (the same idempotency_key, a doubled network
        request rather than a person pressing Send twice) answers with the
        original row's content, not just its id -- _replay() has its own
        conversion path, easy to leave behind a widening of the other two."""
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            payload = {
                "account_id": str(account_id), "kind": "send",
                "idempotency_key": str(uuid.uuid4()), **self._BODY,
            }
            first = client.post("/outbox", json=payload)
            second = client.post("/outbox", json=payload)
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["body_html"] == self._BODY["body_html"]
        assert second.json()["references"] == self._BODY["references"]


class TestPendingSendKeepsItsAttachments:
    """Undo reopens a composer from the staged row, and a composer reopened
    without the attachments looks complete while it is not -- the re-send
    would silently go out without them. The staged row lists each one, and
    its content stays fetchable after the cancel, which is the moment Undo
    asks for it."""

    def _stage_with_attachment(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> tuple[uuid.UUID, dict]:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        data = {
            "account_id": str(account_id), "kind": "send",
            "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
        }
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            resp = client.post(
                "/outbox",
                data={"data": json.dumps(data)},
                files=[("attachments", ("notes.txt", b"attached content", "text/plain"))],
            )
        assert resp.status_code == 201, resp.text
        return account_id, resp.json()

    def test_staging_and_listing_name_each_attachment(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id, created = self._stage_with_attachment(client, migrated_db)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            listed = client.get(f"/outbox/pending?account_id={account_id}").json()

        for row in (created, listed[0]):
            assert [
                (a["filename"], a["content_type"], a["size_bytes"]) for a in row["attachments"]
            ] == [("notes.txt", "text/plain", len(b"attached content"))]

    def test_a_cancelled_sends_attachment_content_is_still_served(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        _account_id, created = self._stage_with_attachment(client, migrated_db)
        attachment_id = created["attachments"][0]["id"]
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            assert client.post(f"/outbox/pending/{created['id']}/cancel").status_code == 204
            resp = client.get(f"/outbox/pending/{created['id']}/attachments/{attachment_id}")
            elsewhere = client.get(f"/outbox/pending/{uuid.uuid4()}/attachments/{attachment_id}")
        assert resp.status_code == 200, resp.text
        assert resp.content == b"attached content"
        assert resp.headers["content-type"].startswith("text/plain")
        # Scoped to its own send: the attachment id alone does not reach it.
        assert elsewhere.status_code == 404


class TestPendingSendAnnouncesItself:
    """PendingSend is MailVerdict's own staging table -- it does not exist
    in outbox at all yet, so PostIMAP's own outbox trigger has nothing to
    fire on. A second connected viewer of the outbox list needs staging
    and cancellation pushed by hand, or the undo countdown never appears
    or disappears for them."""

    def test_staging_a_send_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "connected", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_OUTBOX_TARGET, return_value=migrated_db),
            patch(_OUTBOX_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            )
        assert resp.status_code == 201, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "outbox.updated"]
        assert len(matching) == 1, f"expected one outbox.updated event, got {new_events!r}"

    def test_cancelling_a_staged_send_announces_itself(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        account_id = client.portal.call(_seed_account_and_settings, migrated_db, 30.0)
        with patch(_OUTBOX_TARGET, return_value=migrated_db):
            created = client.post(
                "/outbox",
                json={
                    "account_id": str(account_id), "kind": "send",
                    "to": ["them@example.com"], "subject": "hi", "body_text": "hi",
                },
            ).json()

        event_ring = EventRing()
        client.portal.call(event_ring.add, account_id, "connected", {})
        seq_before = event_ring.get_latest_seq()

        with (
            patch(_OUTBOX_TARGET, return_value=migrated_db),
            patch(_OUTBOX_EVENT_RING_TARGET, return_value=event_ring),
        ):
            resp = client.post(f"/outbox/pending/{created['id']}/cancel")
        assert resp.status_code == 204, resp.text

        new_events = client.portal.call(event_ring.replay_from, seq_before, str(account_id))
        matching = [e for e in new_events if e["event_type"] == "outbox.updated"]
        assert len(matching) == 1, f"expected one outbox.updated event, got {new_events!r}"
