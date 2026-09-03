"""
Compose dialog: the account picker shown once more than one account exists.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui.helpers import select_account, unique_email

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
    return resp.json()


@pytest.fixture(scope="module")
def two_accounts(api_client: httpx.Client) -> list[dict[str, Any]]:
    """Two accounts, distinctly named -- the compose picker's Select does
    not render at all with only one, which is why this defect went unseen:
    every other fixture in this suite seeds a single account."""
    return [
        _create_account(api_client, f"Compose test one {uuid.uuid4().hex[:8]}"),
        _create_account(api_client, f"Compose test two {uuid.uuid4().hex[:8]}"),
    ]


class TestComposeAccountPicker:
    def test_the_from_control_names_the_account_not_its_id(
        self, page: Page, app_server: str, two_accounts: list[dict[str, Any]],
    ) -> None:
        """The regression this guards: the From select only resolves a
        label itself when given an item list, which nothing here passes --
        without it, it renders the account's raw id instead of its name."""
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
        expect(trigger.get_by_text(first["name"], exact=True)).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text(first["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(second["id"], exact=False)).to_have_count(0)
