"""
The application shell: which view owns the sidebar, and the navigation
rail's own way back to the mail view once another page has taken it away.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui.helpers import (
    create_account,
    folder,
    select_account,
    unique_email,
    wait_for_account_active,
    wait_for_folder,
)

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


@pytest.fixture(scope="module")
def second_ui_account(api_client: httpx.Client) -> dict[str, Any]:
    """The Account Order card on Settings only renders with more than one
    account -- the test below needs it mounted to reproduce the
    regression it guards."""
    return create_account(api_client, "nav2")


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

    def test_settings_sidebar_links_still_navigate_away(
        self,
        page: Page,
        app_server: str,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        second_ui_account: dict[str, Any],
    ) -> None:
        """The regression this guards: from Settings, every sidebar nav
        link did nothing -- Next's Link handler ran (it called
        preventDefault) but the transition never committed. An effect on
        the Account Order card fed itself a freshly-built array on every
        render, which reran the effect, which set state, forever; that
        endless stream of ordinary-priority renders starved the
        lower-priority transition Link started, so it never got a turn to
        commit history.pushState. Reproducing it needs a second account,
        which is what makes that card render at all."""
        # A fresh page.goto() is a hard reload, which drops any
        # client-side selection made before it -- the account switcher
        # picked here has to happen on /settings itself, the way a real
        # visit reaches Settings from wherever the sidebar already had
        # selected, for the assertion below to check a known folder
        # rather than whichever account the auto-select effect lands on.
        page.goto(f"{app_server}/settings")
        expect(page.get_by_role("heading", name="Settings")).to_be_visible(timeout=15_000)
        select_account(page, ui_account)

        # Scoped to the sidebar: the Settings page's own section-jump nav
        # (SectionNav in settings-page.tsx) carries an identically-named
        # "Mail" anchor to its own #mail heading.
        page.locator('[data-slot="sidebar-footer"]').get_by_role(
            "link", name="Mail", exact=True,
        ).click()
        expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=15_000)

    def test_collapsing_the_rail_hides_the_calendar_mini_month_instead_of_squeezing_it(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: collapsing the sidebar rail on the
        calendar route squeezed the mini-month grid and the per-calendar
        checkbox list into the icon-width rail -- the month's rows
        overlapped and the checkboxes lost their labels. Collapsing now
        hides that content instead of mangling it, and expanding again
        brings it straight back."""
        page.goto(f"{app_server}/calendar")
        mini_month_title = page.get_by_test_id("mini-month-title")
        expect(mini_month_title).to_be_visible(timeout=15_000)

        toggle = page.get_by_role("button", name="Toggle Sidebar", exact=True)
        toggle.click()
        expect(mini_month_title).not_to_be_visible(timeout=8_000)

        toggle.click()
        expect(mini_month_title).to_be_visible(timeout=8_000)

    def test_slash_focuses_the_header_search_and_enter_jumps_to_results(
        self, page: Page, app_server: str, ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The header search field (app-header.tsx): `/` focuses it from
        anywhere, and Enter jumps to the full search page with the query
        already applied."""
        page.goto(f"{app_server}/")
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier tests in this module have created a second account of
        # their own by the time this one runs.
        select_account(page, ui_account)
        expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=15_000)

        search_input = page.get_by_role("textbox", name="Search mail", exact=True)
        expect(search_input).not_to_be_focused()

        page.keyboard.press("/")
        expect(search_input).to_be_focused()

        query = f"header-search-{uuid.uuid4().hex[:8]}"
        search_input.fill(query)
        page.keyboard.press("Enter")

        expect(page).to_have_url(re.compile(rf"/search\?q={query}"))
        expect(page.get_by_placeholder("Search messages…")).to_have_value(
            query, timeout=10_000,
        )

    def test_question_mark_opens_the_shortcuts_overlay(
        self, page: Page, app_server: str, ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The `?` cheat sheet (ShortcutsOverlay, layout.tsx), reachable
        from any page and toggled by the same key that opens it."""
        page.goto(f"{app_server}/")
        select_account(page, ui_account)
        expect(folder(page, inbox_folder["id"])).to_be_visible(timeout=15_000)

        page.keyboard.press("?")
        dialog = page.get_by_role("dialog", name="Keyboard shortcuts")
        expect(dialog).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text("Focus search", exact=True)).to_be_visible()

        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible(timeout=10_000)
