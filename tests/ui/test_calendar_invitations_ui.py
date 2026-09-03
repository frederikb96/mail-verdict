"""
The mail-side half of calendar intake, driven through the interface rather
than seeded directly into dav_objects the way tests/pg/test_invitations_api_pg.py
does -- a real RFC822 message with a text/calendar part, delivered over LMTP
the same way tests/setup/mail_delivery.py delivers everything else, is what
actually exercises the calendar_intake listener and PostIMAP's own
notifications, neither of which a seeded row ever reaches.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
    wait_for_mailpit_message,
)
from tests.setup.dav_helpers import create_calendar, discover
from tests.setup.mail_delivery import deliver_message
from tests.ui.helpers import mail_row, wait_for_account_active, wait_for_folder

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


def _build_invitation_eml(
    *, organizer_email: str, attendee_email: str, summary: str, uid: str, method: str = "REQUEST",
) -> bytes:
    """A REQUEST three days out, one attendee (the recipient) and the
    organizer as chair -- the same shape .claude/deliver_invitation.py
    uses for manual driving, rebuilt here so the committed suite carries
    no dependency on that personal-path script."""
    start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=3, hours=12)
    )
    end = start + timedelta(hours=1)
    fmt = "%Y%m%dT%H%M%SZ"
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN", f"METHOD:{method}",
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}", f"SUMMARY:{summary}",
        "LOCATION:Cafe Central", "SEQUENCE:0",
        f"ORGANIZER;CN=Bob Example:mailto:{organizer_email}",
        f"ATTENDEE;CN=Attendee;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
        f"RSVP=TRUE:mailto:{attendee_email}",
        f"ATTENDEE;CN=Bob Example;ROLE=CHAIR;PARTSTAT=ACCEPTED:mailto:{organizer_email}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])
    msg = EmailMessage()
    msg["From"] = f"Bob Example <{organizer_email}>"
    msg["To"] = attendee_email
    msg["Subject"] = f"Invitation: {summary}"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.com")
    msg.set_content(f"You are invited to {summary}.")
    msg.add_alternative(ics, subtype="calendar", params={"method": method, "charset": "utf-8"})
    # EmailMessage.as_bytes() uses bare LF by default -- LMTP delivery
    # needs CRLF, the same normalisation load_corpus() applies to fixtures.
    raw = msg.as_bytes()
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _create_attendee_identity(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int], prefix: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A real, active mail account plus an identity on it -- the ATTENDEE
    address an invitation names has to belong to an identity before the
    invitation card can resolve `own_address` at all."""
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email(prefix)
    resp = api_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",  # Mailpit accepts any SMTP AUTH credentials
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])
    account["email"] = email

    resp = api_client.post("/api/identities", json={"account_id": account["id"], "address": email})
    assert resp.status_code == 201, resp.text
    return account, resp.json()


def _find_message_by_subject(
    api_client: httpx.Client, account_id: str, folder_id: str, subject: str,
) -> dict[str, Any] | None:
    resp = api_client.get(f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id})
    assert resp.status_code == 200, resp.text
    return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def ui_calendar_owner(radicale_base_url: str) -> str:
    """A calendar on the real Radicale server, owned by a fresh principal --
    the dav_account below points at it."""
    username = f"invite-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_calendar(client, principal, "personal", "Personal")
    return username


@pytest.fixture(scope="module")
def dav_account(api_client: httpx.Client, ui_calendar_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": ui_calendar_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def calendar_collection(api_client: httpx.Client, dav_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, dav_account["id"], "Personal")


class TestCalendarInvitationsUi:
    """Shares one DAV account and its one synced calendar; each test uses
    its own mail account and identity so an unlinked invitation and a
    pre-linked one never compete over the same attendee address."""

    def test_invitation_intake_manual_then_accept_sends_an_itip_reply(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        mailpit_http_url: str,
        calendar_collection: dict[str, Any],
    ) -> None:
        """An unlinked invitation, added to a chosen calendar by hand, then
        accepted: the reply reaches the mail sink as an iTIP REPLY and the
        stored object carries the accepted participation status."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        account, identity = _create_attendee_identity(
            api_client, dovecot_endpoint, "attendee-manual",
        )

        # Only the identity, not the intake mode -- GET /calendar/events/{id}
        # resolves "your own" partstat from calendar_prefs.identity_id
        # regardless of intake, so this is what lets the RSVP control
        # resolve once the invitation is imported by hand below; leaving
        # intake at its default ("none") is what keeps the listener from
        # auto-importing, which is the manual path this test is for.
        resp = api_client.patch(
            f"/api/calendars/{calendar_collection['id']}", json={"identity_id": identity["id"]},
        )
        assert resp.status_code == 200, resp.text

        summary = f"Lunch invite {uuid.uuid4()}"
        uid = f"invite-{uuid.uuid4()}@example.com"
        organizer_email = "bob@example.com"
        eml = _build_invitation_eml(
            organizer_email=organizer_email, attendee_email=account["email"],
            summary=summary, uid=uid,
        )
        deliver_message(eml, host, lmtp_port, sender=organizer_email, recipient=account["email"])

        inbox = wait_for_folder(api_client, str(account["id"]), "INBOX")
        subject = f"Invitation: {summary}"
        message = wait_for(
            lambda: _find_message_by_subject(api_client, account["id"], inbox["id"], subject),
            description="Invitation mail synced into INBOX",
        )

        # No calendar link exists for this identity yet -- confirms the
        # manual path's own starting state before driving the interface.
        def _unlinked() -> dict[str, Any] | None:
            body = api_client.get(f"/api/calendar/invitations/{message['id']}").json()
            return body if body["status"] == "unlinked" else None

        wait_for(_unlinked, description="Invitation reads as unlinked before any import")

        page.goto(app_server)
        mail_row(page, message["id"]).click()

        expect(page.get_by_text("Not in a calendar yet", exact=True)).to_be_visible(timeout=15_000)
        combobox = page.get_by_role("combobox").filter(has_text="Add to calendar")
        combobox.click()
        page.get_by_role("option", name=calendar_collection["display_name"], exact=True).click()
        page.get_by_role("button", name="Add", exact=True).click()

        expect(
            page.get_by_text(f"Added to {calendar_collection['display_name']}", exact=False)
        ).to_be_visible(timeout=15_000)

        accept_button = page.get_by_role("button", name="Accept", exact=True)
        expect(accept_button).to_be_visible(timeout=10_000)
        accept_button.click()
        expect(page.get_by_text("Reply sent", exact=True)).to_be_visible(timeout=20_000)

        mailpit_message = wait_for_mailpit_message(mailpit_http_url, f"Accepted: {summary}")
        # Mailpit's own /message/{ID} JSON summarises each attachment's
        # Content-Type down to the bare "text/calendar", dropping every
        # parameter -- the raw source is what still carries `method=REPLY`.
        raw = httpx.get(
            f"{mailpit_http_url}/api/v1/message/{mailpit_message['ID']}/raw", timeout=10.0,
        )
        assert raw.status_code == 200, raw.text
        assert "method=REPLY" in raw.text, (
            f"expected an iTIP REPLY attachment on the reply mail; raw source:\n{raw.text}"
        )

        def _accepted() -> dict[str, Any] | None:
            invitation = api_client.get(f"/api/calendar/invitations/{message['id']}").json()
            if invitation["object_id"] is None:
                return None
            event = api_client.get(f"/api/calendar/events/{invitation['object_id']}").json()
            return event if event["partstat"] == "accepted" else None

        wait_for(_accepted, description="Stored object carries the accepted participation status")

    def test_invitation_intake_automatic_imports_with_no_interaction(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        calendar_collection: dict[str, Any],
    ) -> None:
        """With an identity already linked to a calendar for automatic
        intake, the card reports the invitation was added there the
        moment the mail is opened -- no Select, no Add click."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        account, identity = _create_attendee_identity(api_client, dovecot_endpoint, "attendee-auto")

        resp = api_client.patch(
            f"/api/calendars/{calendar_collection['id']}",
            json={"identity_id": identity["id"], "intake": "import_and_link"},
        )
        assert resp.status_code == 200, resp.text

        summary = f"Standing sync {uuid.uuid4()}"
        uid = f"invite-{uuid.uuid4()}@example.com"
        organizer_email = "bob@example.com"
        eml = _build_invitation_eml(
            organizer_email=organizer_email, attendee_email=account["email"],
            summary=summary, uid=uid,
        )
        deliver_message(eml, host, lmtp_port, sender=organizer_email, recipient=account["email"])

        inbox = wait_for_folder(api_client, str(account["id"]), "INBOX")
        subject = f"Invitation: {summary}"
        message = wait_for(
            lambda: _find_message_by_subject(api_client, account["id"], inbox["id"], subject),
            description="Invitation mail synced into INBOX",
        )

        def _auto_imported() -> dict[str, Any] | None:
            resp = api_client.get(f"/api/calendar/invitations/{message['id']}")
            body = resp.json() if resp.status_code == 200 else None
            return body if body and body["status"] == "imported" else None

        wait_for(
            _auto_imported, timeout_s=30.0, description="Listener auto-imported the invitation",
        )

        page.goto(app_server)
        mail_row(page, message["id"]).click()

        expect(
            page.get_by_text(f"Added to {calendar_collection['display_name']}", exact=False)
        ).to_be_visible(timeout=15_000)
