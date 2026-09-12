"""
Compose dialog: one From control across every account, showing exactly
which address a message will send from.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui.helpers import select_account, unique_email, wait_for

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
)


def _create_account(api_client: httpx.Client, name: str) -> dict[str, Any]:
    email = unique_email("compose")
    resp = api_client.post(
        "/api/accounts",
        json={
            "name": name,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": "unused",
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    account["email"] = email
    return account


@pytest.fixture(scope="module")
def two_accounts(api_client: httpx.Client) -> list[dict[str, Any]]:
    """Two accounts, distinctly named, neither with an identity of its
    own -- the From control still has to offer both, using each account's
    own address as its own send-time fallback resolution does."""
    return [
        _create_account(api_client, f"Compose test one {uuid.uuid4().hex[:8]}"),
        _create_account(api_client, f"Compose test two {uuid.uuid4().hex[:8]}"),
    ]


class TestComposeFromControl:
    def test_the_from_control_shows_the_address_not_the_account_name_or_id(
        self, page: Page, app_server: str, two_accounts: list[dict[str, Any]],
    ) -> None:
        """The regression this guards: a separate account picker named
        only the account, leaving a second, address-only picker (for the
        cases where the account had more than one identity) to name which
        address actually sends. One control now does both, and always
        shows an address -- including for an account with no identity of
        its own, where it falls back to accounts.imap_user, the same
        address resolve_send_from_addr resolves to server-side."""
        first, second = two_accounts

        page.goto(app_server)
        # Compose's From starts on the sidebar's currently selected account
        # -- explicit, rather than trusting whichever account a fresh page
        # load auto-selects, which is only ever `first` when this account
        # sorts before every other account the shared test database holds.
        select_account(page, first)
        # exact=True: substring matching would otherwise also resolve the
        # sidebar's own account-switcher trigger, now labelled "Compose
        # test one ..." since that account is the one just selected.
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        expect(dialog).to_be_visible(timeout=15_000)

        trigger = dialog.locator('[data-slot="select-trigger"]')
        expect(trigger).to_be_visible(timeout=10_000)
        expect(trigger.get_by_text(first["email"], exact=True)).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text(first["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(second["id"], exact=False)).to_have_count(0)

    def test_two_identities_sharing_a_display_name_are_distinguished_by_address(
        self, page: Page, app_server: str, api_client: httpx.Client,
    ) -> None:
        """The regression this guards: the From select showed only a
        display name, so two identities sharing one -- an ordinary setup,
        not an edge case -- were indistinguishable in the picker. Also
        proves the picked entry, not merely the first or default one, is
        what a send actually goes out as."""
        account = _create_account(api_client, f"Compose shared name {uuid.uuid4().hex[:8]}")
        first_addr = unique_email("shared-a")
        second_addr = unique_email("shared-b")
        for address in (first_addr, second_addr):
            resp = api_client.post(
                "/api/identities",
                json={
                    "account_id": account["id"], "address": address,
                    "display_name": "Shared Name",
                },
            )
            assert resp.status_code == 201, resp.text

        page.goto(app_server)
        select_account(page, account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        expect(dialog).to_be_visible(timeout=15_000)

        trigger = dialog.locator('[data-slot="select-trigger"]')
        trigger.click()
        first_option = page.get_by_role("option", name=f"Shared Name <{first_addr}>", exact=True)
        second_option = page.get_by_role(
            "option", name=f"Shared Name <{second_addr}>", exact=True,
        )
        expect(first_option).to_be_visible(timeout=8_000)
        expect(second_option).to_be_visible()
        second_option.click()
        expect(
            trigger.get_by_text(f"Shared Name <{second_addr}>", exact=True)
        ).to_be_visible(timeout=8_000)

        subject = f"picked address {uuid.uuid4()}"
        dialog.get_by_role("combobox", name="To", exact=True).fill("someone@example.com")
        page.keyboard.press("Enter")
        dialog.get_by_role("textbox", name="Subject", exact=True).fill(subject)
        dialog.get_by_test_id("mail-editor-body").fill("hello")
        dialog.get_by_role("button", name="Send", exact=True).click()

        def _sent_from() -> str | None:
            rows = api_client.get(f"/api/outbox?account_id={account['id']}").json()
            match = next((r for r in rows if r.get("subject") == subject), None)
            return match["from_addr"] if match else None

        assert wait_for(_sent_from, description=f"Outbox row for {subject!r}") == second_addr
