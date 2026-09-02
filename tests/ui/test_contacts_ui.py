"""
Contacts UI actions against a real Radicale server -- the contact editor's
own create path, which the e2e layer does not exercise since it never
drives the actual form.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import (
    unique_email,
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
)
from tests.setup.dav_helpers import create_addressbook, discover
from tests.ui.helpers import wait_for_account_active

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def ui_addressbook_owner(radicale_base_url: str) -> str:
    """An address book on the real Radicale server, owned by a fresh
    principal -- the dav_account below points at it."""
    username = f"card-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_addressbook(client, principal, "friends", "Friends")
    return username


@pytest.fixture(scope="module")
def dav_account(api_client: httpx.Client, ui_addressbook_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": ui_addressbook_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def addressbook(api_client: httpx.Client, dav_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, dav_account["id"], "Friends")


@pytest.fixture(scope="module")
def ui_account(api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int]) -> dict[str, Any]:
    """An active mail account -- the compose dialog's recipient field is
    only reachable from a real account's compose flow."""
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email("ui")
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
    return account


@pytest.fixture(scope="module")
def seeded_contact(api_client: httpx.Client, addressbook: dict[str, Any]) -> dict[str, Any]:
    """A contact the compose autocomplete can actually find -- "ann" is a
    substring of both its name and its email."""
    resp = api_client.post(
        "/api/contacts",
        json={
            "addressbook_id": addressbook["id"],
            "summary": "Anna Testerson",
            "emails": [{"email": "anna.testerson@example.com"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestContactsUi:
    def test_creating_a_contact_from_the_editor_succeeds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        addressbook: dict[str, Any],
    ) -> None:
        """The regression this guards: the default address book was read on
        first mount, before address books had loaded, so every create sent
        an empty addressbook_id and was rejected with the sheet left open
        and no feedback at all."""
        name = f"UI Contact Test {uuid.uuid4()}"
        email = f"{uuid.uuid4().hex[:8]}@example.com"

        page.goto(f"{app_server}/contacts")
        page.get_by_role("button", name="New contact", exact=True).click()

        name_input = page.get_by_label("Name", exact=True)
        expect(name_input).to_be_visible(timeout=15_000)
        name_input.fill(name)
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_role("button", name="Save", exact=True).click()

        expect(name_input).to_be_hidden(timeout=10_000)
        expect(page.get_by_text(name, exact=True)).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            resp = api_client.get("/api/contacts", params={"q": name})
            assert resp.status_code == 200, resp.text
            contacts = resp.json()["contacts"]
            return contacts[0] if contacts else None

        contact = wait_for(_created, description="Created contact synced back")
        assert contact["addressbook_id"] == addressbook["id"]

    def test_recipient_field_arrow_down_enter_commits_the_highlighted_suggestion(
        self,
        page: Page,
        app_server: str,
        ui_account: dict[str, Any],
        seeded_contact: dict[str, Any],
    ) -> None:
        """The regression this guards: Enter always committed the raw typed
        text as a chip, regardless of whether a suggestion was highlighted
        -- so arrow-down-then-enter on "ann" produced a literal "ann" chip
        instead of the highlighted anna.testerson@example.com, and
        parseAddressList happily accepted it, queuing a send that could
        only fail later."""
        page.goto(app_server)
        page.get_by_role("button", name="Compose").click()

        to_field = page.get_by_role("combobox", name="To")
        expect(to_field).to_be_visible(timeout=15_000)
        to_field.fill("ann")

        suggestion = page.get_by_role("option", name="anna.testerson@example.com")
        expect(suggestion).to_be_visible(timeout=10_000)
        to_field.press("ArrowDown")
        to_field.press("Enter")

        expect(page.get_by_text("anna.testerson@example.com", exact=True)).to_be_visible(
            timeout=10_000
        )
        expect(page.get_by_text("ann", exact=True)).to_have_count(0)
        expect(to_field).to_have_value("")

    def test_recipient_field_rejects_unparseable_text_visibly(
        self, page: Page, app_server: str, ui_account: dict[str, Any],
    ) -> None:
        """Neither of the two silent failures: text that isn't an address
        is not turned into a chip that will only fail at send time, and it
        is not dropped without a trace either -- a toast names exactly what
        was rejected and why."""
        page.goto(app_server)
        page.get_by_role("button", name="Compose").click()

        to_field = page.get_by_role("combobox", name="To")
        expect(to_field).to_be_visible(timeout=15_000)
        to_field.fill("not-an-address")
        to_field.press("Enter")

        expect(
            page.get_by_text("Not a valid email address: not-an-address", exact=True)
        ).to_be_visible(timeout=10_000)
        expect(page.get_by_text("not-an-address", exact=True)).to_have_count(0)
