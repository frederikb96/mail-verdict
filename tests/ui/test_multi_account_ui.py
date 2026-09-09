"""
The paths that only exist once more than one account is configured: the
account picker actually swaps which account's own mail is shown, the
unified view's merged folder row says which accounts it merged, a search
can be scoped across every account rather than just the sidebar's current
one, and an action taken against one account's message never touches the
other account's.

Every test here creates its own pair of accounts -- a shared two-account
fixture would tie every test in this module to the same pair, and a test
that leaves one of them in an unexpected state (a message moved, read) is
exactly what an isolated fixture per test avoids.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    create_account,
    folder_button,
    mail_row,
    select_account,
    wait_for,
    wait_for_folder,
)


def _deliver(
    dovecot_endpoint: tuple[str, int, int],
    api_client: httpx.Client,
    account_id: str,
    folder_id: str,
    recipient: str,
    subject: str,
) -> dict[str, Any]:
    host, _imap_port, lmtp_port = dovecot_endpoint
    message = build_eml(
        sender="sender@example.com", recipient=recipient, subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>",
    )
    deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=recipient)

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id},
        )
        assert resp.status_code == 200, resp.text
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    return wait_for(
        _find, timeout_s=45.0, description=f"{subject!r} synced into folder {folder_id}",
    )


def _set_unified_name(api_client: httpx.Client, folder_id: str, unified_name: str) -> None:
    resp = api_client.patch(
        f"/api/folders/{folder_id}/prefs", json={"unified_name": unified_name},
    )
    assert resp.status_code == 200, resp.text


class TestAccountPicker:
    """The switcher is the one control every other multi-account path
    depends on -- if it silently kept showing the previous account's
    folders, everything downstream would too."""

    def test_switching_accounts_shows_only_that_accounts_own_message(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = create_account(api_client, "picker-a")
        account_b = create_account(api_client, "picker-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")

        msg_a = _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"Picker A {uuid.uuid4()}",
        )
        msg_b = _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"Picker B {uuid.uuid4()}",
        )

        page.goto(app_server)
        select_account(page, account_a)
        folder_button(page, inbox_a["id"]).click()
        expect(mail_row(page, msg_a["id"])).to_be_visible(timeout=15_000)
        expect(mail_row(page, msg_b["id"])).to_have_count(0)

        select_account(page, account_b)
        folder_button(page, inbox_b["id"]).click()
        expect(mail_row(page, msg_b["id"])).to_be_visible(timeout=15_000)
        expect(mail_row(page, msg_a["id"])).to_have_count(0)


class TestUnifiedViewIdentifiesEachAccount:
    """A merged folder's row used to say only how MANY accounts fed it (a
    hover-only tooltip); this is the visible, no-hover-needed form."""

    def test_unified_folder_row_shows_both_accounts_and_both_messages(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = create_account(api_client, "unified-id-a")
        account_b = create_account(api_client, "unified-id-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")

        unified_name = f"Unified Inbox {uuid.uuid4().hex[:8]}"
        _set_unified_name(api_client, inbox_a["id"], unified_name)
        _set_unified_name(api_client, inbox_b["id"], unified_name)

        msg_a = _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"Unified id A {uuid.uuid4()}",
        )
        msg_b = _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"Unified id B {uuid.uuid4()}",
        )

        def _group_ready() -> bool | None:
            resp = api_client.get("/api/unified/folders")
            assert resp.status_code == 200, resp.text
            groups = {g["unified_name"]: g for g in resp.json()}
            group = groups.get(unified_name)
            return True if group is not None and len(group["folders"]) == 2 else None

        wait_for(_group_ready, timeout_s=30.0, description="the unified folder group merged")

        page.goto(app_server)
        trigger = page.locator('[data-slot="sidebar-header"]').get_by_role("button").first
        trigger.click()
        page.locator('[data-slot="dropdown-menu-item"]').get_by_text(
            "Unified View", exact=True,
        ).click()

        folder_row = page.locator('[data-testid="folder"]').filter(has_text=unified_name)
        expect(folder_row).to_be_visible(timeout=15_000)
        # Both accounts' emoji (or the mail-icon fallback glyph) render
        # inline in the row itself -- not only in a title attribute that
        # would need a hover to read.
        expect(folder_row.get_by_title(account_a["name"], exact=True)).to_have_count(1)
        expect(folder_row.get_by_title(account_b["name"], exact=True)).to_have_count(1)

        folder_row.get_by_role("button").first.click()
        expect(mail_row(page, msg_a["id"])).to_be_visible(timeout=15_000)
        expect(mail_row(page, msg_b["id"])).to_be_visible(timeout=15_000)


class TestSearchAcrossAccounts:
    """The search page's own account scope, independent of whatever the
    sidebar currently shows -- see search-prefs.ts's searchAccountIdAtom."""

    def test_all_accounts_scope_finds_and_badges_a_message_from_each(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        marker = f"searchscopemarker{uuid.uuid4().hex[:8]}"
        account_a = create_account(api_client, "search-scope-a")
        account_b = create_account(api_client, "search-scope-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")

        _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"{marker} from A",
        )
        _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"{marker} from B",
        )

        page.goto(app_server)
        # Land the sidebar on account_a specifically -- the point of this
        # test is that the search page's own scope is independent of it,
        # so search still has to reach account_b's message from here
        # without switching the sidebar into its separate Unified View.
        select_account(page, account_a)

        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        search_input.fill(marker)

        scope = page.locator('[data-slot="select-trigger"]')
        rows = page.locator('[data-testid="search-result-row"]')

        # Default scope is every account -- both messages reach the
        # sidebar's non-current account, badged with which is which.
        expect(rows).to_have_count(2, timeout=15_000)
        expect(rows.filter(has_text=account_a["name"])).to_have_count(1)
        expect(rows.filter(has_text=account_b["name"])).to_have_count(1)

        # Narrowing the scope control itself to one account is what
        # actually proves it's interactive, not just a default that
        # happens to already cover this.
        scope.click()
        page.get_by_role("option", name=account_a["name"], exact=True).click()
        expect(rows).to_have_count(1, timeout=15_000)
        expect(rows.filter(has_text=account_a["name"])).to_have_count(1)

        scope.click()
        page.get_by_role("option", name="All accounts", exact=True).click()
        expect(rows).to_have_count(2, timeout=15_000)


class TestPerAccountActionStaysScoped:
    """A row action taken while one account is selected must never reach
    the other account's own message, however similar the two look."""

    def test_trashing_in_one_account_leaves_the_others_message_untouched(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = create_account(api_client, "action-scope-a")
        account_b = create_account(api_client, "action-scope-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")

        msg_a = _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"Action scope A {uuid.uuid4()}",
        )
        msg_b = _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"Action scope B {uuid.uuid4()}",
        )

        page.goto(app_server)
        select_account(page, account_b)
        folder_button(page, inbox_b["id"]).click()
        row_b = mail_row(page, msg_b["id"])
        expect(row_b).to_be_visible(timeout=15_000)
        row_b.hover()
        # Threading is on by default, which appends " (latest message in
        # thread)" to every row's title regardless of whether it actually
        # has thread siblings -- match the prefix, not the exact string.
        row_b.get_by_title(re.compile(r"^Move to trash")).click()

        def _trashed() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/messages/{msg_b['id']}").json()
            return detail if detail["folder_id"] != inbox_b["id"] else None

        wait_for(_trashed, timeout_s=15.0, description="account B's message moved to trash")

        detail_a = api_client.get(f"/api/messages/{msg_a['id']}").json()
        assert detail_a["folder_id"] == inbox_a["id"], (
            "trashing account B's message also moved account A's -- the action leaked "
            "across accounts"
        )
