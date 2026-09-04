"""
The application shell: which view owns the sidebar, and the navigation
rail's own way back to the mail view once another page has taken it away.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui.helpers import folder, unique_email, wait_for_account_active, wait_for_folder

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
)


@pytest.fixture(scope="module")
def ui_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email("nav")
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
def inbox_folder(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "INBOX")


class TestNavigationShellUi:
    def test_contacts_view_does_not_render_the_mail_folder_tree(
        self, page: Page, app_server: str, inbox_folder: dict[str, Any],
    ) -> None:
        """The regression this guards: the contacts view rendered the mail
        sidebar's folder tree underneath its own list panel -- a folder
        that belongs only to the mail view showed up wherever the reader
        went next."""
        page.goto(f"{app_server}/")
        expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=15_000)

        page.goto(f"{app_server}/contacts")
        expect(page.get_by_placeholder("Search contacts")).to_be_visible(timeout=15_000)

        # The account/folder queries this would need are the same ones the
        # mail route above just proved resolve within its own timeout, and
        # they run regardless of route -- asserting to_have_count(0) without
        # waiting through that same window would pass the instant before
        # they resolve, whether or not the folder tree is actually excluded
        # here. Polling for the positive outcome and requiring it to time
        # out is what actually proves it never appears.
        with pytest.raises(AssertionError):
            expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=8_000)

    def test_mail_entry_sits_between_search_and_calendar_and_returns_to_mail(
        self, page: Page, app_server: str, inbox_folder: dict[str, Any],
    ) -> None:
        """The regression this guards: the navigation rail had no Mail
        entry at all, so once on Search or Calendar there was no way back
        to the mail view."""
        page.goto(f"{app_server}/search")
        expect(page.get_by_role("heading", name="Search")).to_be_visible(timeout=15_000)

        footer_links = page.locator('[data-slot="sidebar-footer"]').get_by_role("link")
        names = footer_links.all_text_contents()
        assert names.index("Mail") == names.index("Search") + 1
        assert names.index("Calendar") == names.index("Mail") + 1

        page.get_by_role("link", name="Mail", exact=True).click()
        expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=15_000)
