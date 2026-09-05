"""
The bulk panel's "Move to" over a selection that spans two accounts --
only reachable in the unified view, where a hand-picked selection (never
a predicate, which is always minted over one real account's folder) can
name ids belonging to different accounts.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from playwright.sync_api import Page, Response, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    mail_row,
    unique_email,
    wait_for,
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


def _create_account(api_client: httpx.Client, email: str) -> dict[str, Any]:
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
            "smtp_password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])
    account["email"] = email
    return account


def _set_unified_name(api_client: httpx.Client, folder_id: str, unified_name: str) -> None:
    resp = api_client.patch(
        f"/api/folders/{folder_id}/prefs", json={"unified_name": unified_name},
    )
    assert resp.status_code == 200, resp.text


def _create_folder(api_client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    resp = api_client.post(f"/api/accounts/{account_id}/folders", json={"name": name})
    assert resp.status_code == 201, resp.text
    return wait_for_folder(api_client, account_id, resp.json()["imap_name"])


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


def _select_unified_view(page: Page) -> None:
    """Same control select_account() drives, picking the fixed "Unified
    View" entry instead of an account by name."""
    trigger = page.locator('[data-slot="sidebar-header"]').get_by_role("button").first
    opened_sheet = trigger.is_hidden()
    if opened_sheet:
        page.locator('[data-slot="sidebar-trigger"]').click()
        expect(trigger).to_be_visible(timeout=10_000)
    trigger.click()
    page.locator('[data-slot="dropdown-menu-item"]').get_by_text(
        "Unified View", exact=True,
    ).click()
    if opened_sheet:
        sheet = page.locator('[data-slot="sheet-portal"]')
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0, timeout=10_000)


def _open_unified_folder(page: Page, unified_name: str) -> None:
    page.locator('[data-testid="folder"]').filter(has_text=unified_name).get_by_role(
        "button",
    ).first.click()


class TestUnifiedSelectionMoveUi:
    """One test proves both halves of the same defect: the panel calling
    the wrong endpoint for a unified-view folder list (a 422 on every open),
    and a cross-account move sending one account's folder id to the other
    (a 400, and the other account's move already applied)."""

    def test_move_to_a_merged_folder_resolves_each_message_into_its_own_accounts_folder(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = _create_account(api_client, unique_email("unified-a"))
        account_b = _create_account(api_client, unique_email("unified-b"))
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")

        unified_inbox_name = f"Unified Inbox {uuid.uuid4().hex[:8]}"
        _set_unified_name(api_client, inbox_a["id"], unified_inbox_name)
        _set_unified_name(api_client, inbox_b["id"], unified_inbox_name)

        target_a = _create_folder(api_client, account_a["id"], f"Target-{uuid.uuid4().hex[:8]}")
        target_b = _create_folder(api_client, account_b["id"], f"Target-{uuid.uuid4().hex[:8]}")
        unified_target_name = f"Unified Target {uuid.uuid4().hex[:8]}"
        _set_unified_name(api_client, target_a["id"], unified_target_name)
        _set_unified_name(api_client, target_b["id"], unified_target_name)

        msg_a = _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"Unified move A {uuid.uuid4()}",
        )
        msg_b = _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"Unified move B {uuid.uuid4()}",
        )

        # /api/unified/folders groups by unified_name lazily -- wait for
        # both merged groups to actually report two accounts each before
        # driving the browser, rather than racing the grouping query.
        def _both_groups_ready() -> bool | None:
            resp = api_client.get("/api/unified/folders")
            assert resp.status_code == 200, resp.text
            groups = {g["unified_name"]: g for g in resp.json()}
            inbox_group = groups.get(unified_inbox_name)
            target_group = groups.get(unified_target_name)
            ready = (
                inbox_group is not None and len(inbox_group["folders"]) == 2
                and target_group is not None and len(target_group["folders"]) == 2
            )
            return True if ready else None

        wait_for(
            _both_groups_ready, timeout_s=30.0, description="both unified folder groups merged",
        )

        page.goto(app_server)
        _select_unified_view(page)
        _open_unified_folder(page, unified_inbox_name)

        row_a = mail_row(page, msg_a["id"])
        row_b = mail_row(page, msg_b["id"])
        expect(row_a).to_be_visible(timeout=15_000)
        expect(row_b).to_be_visible(timeout=15_000)

        row_a.hover()
        row_a.get_by_role("checkbox").click()
        row_b.hover()
        row_b.get_by_role("checkbox").click()
        expect(page.get_by_text("2 selected", exact=True)).to_be_visible(timeout=10_000)

        bad_responses: list[Response] = []
        page.on(
            "response",
            lambda resp: bad_responses.append(resp)
            if "/accounts/unified/folder-order" in resp.url
            else None,
        )

        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Move to", exact=True,
        ).click()
        move_item = page.get_by_role("menuitem", name=unified_target_name, exact=True)
        expect(move_item).to_be_visible(timeout=10_000)
        assert not bad_responses, (
            f"the bulk panel called the single-account folder-order endpoint from "
            f"the unified view: {[r.url for r in bad_responses]}"
        )
        move_item.click()

        def _both_moved() -> bool | None:
            detail_a = api_client.get(f"/api/messages/{msg_a['id']}").json()
            detail_b = api_client.get(f"/api/messages/{msg_b['id']}").json()
            moved = (
                detail_a["folder_id"] == target_a["id"] and not detail_a["pending_sync"]
                and detail_b["folder_id"] == target_b["id"] and not detail_b["pending_sync"]
            )
            return True if moved else None

        wait_for(
            _both_moved, timeout_s=20.0,
            description="both messages moved into their own account's target folder",
        )


class TestSelectionSurvivesItsRowLeavingTheCache:
    """A ticked row's account travels with the selection itself now, not
    re-derived from a list cache at bulk-action time -- so a row that has
    left every list cache (a live-update splice, in the real mechanism
    below) stays actionable rather than being silently dropped."""

    def test_bulk_action_still_reaches_a_row_moved_out_of_the_list_after_it_was_ticked(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = _create_account(api_client, unique_email("unified-evict-a"))
        account_b = _create_account(api_client, unique_email("unified-evict-b"))
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")
        elsewhere_a = _create_folder(
            api_client, account_a["id"], f"Elsewhere-{uuid.uuid4().hex[:8]}",
        )

        unified_inbox_name = f"Unified Inbox {uuid.uuid4().hex[:8]}"
        _set_unified_name(api_client, inbox_a["id"], unified_inbox_name)
        _set_unified_name(api_client, inbox_b["id"], unified_inbox_name)

        msg_a = _deliver(
            dovecot_endpoint, api_client, account_a["id"], inbox_a["id"],
            account_a["email"], f"Unified evict A {uuid.uuid4()}",
        )
        msg_b = _deliver(
            dovecot_endpoint, api_client, account_b["id"], inbox_b["id"],
            account_b["email"], f"Unified evict B {uuid.uuid4()}",
        )

        def _group_ready() -> bool | None:
            resp = api_client.get("/api/unified/folders")
            assert resp.status_code == 200, resp.text
            groups = {g["unified_name"]: g for g in resp.json()}
            group = groups.get(unified_inbox_name)
            return True if group is not None and len(group["folders"]) == 2 else None

        wait_for(_group_ready, timeout_s=30.0, description="the unified folder group merged")

        page.goto(app_server)
        _select_unified_view(page)
        _open_unified_folder(page, unified_inbox_name)

        row_a = mail_row(page, msg_a["id"])
        row_b = mail_row(page, msg_b["id"])
        expect(row_a).to_be_visible(timeout=15_000)
        expect(row_b).to_be_visible(timeout=15_000)

        # Tick both while both rows are still genuinely in the list --
        # this is what carries account_a/account_b into the selection.
        row_a.hover()
        row_a.get_by_role("checkbox").click()
        row_b.hover()
        row_b.get_by_role("checkbox").click()
        expect(page.get_by_text("2 selected", exact=True)).to_be_visible(timeout=10_000)

        # Move msg_a out of the merged folder through the API directly,
        # entirely outside this selection gesture -- the live-update this
        # produces (mail.updated, folder_id changed) is what
        # removeMailFromAllListCaches reacts to, splicing msg_a's row out
        # of every list cache including the unified one. The selection
        # itself is untouched: msg_a is still ticked, its row is simply
        # gone from wherever a bulk action might otherwise look it up.
        resp = api_client.post(
            f"/api/messages/{msg_a['id']}/action",
            json={"action": "move", "target_folder_id": elsewhere_a["id"]},
        )
        assert resp.status_code == 200, resp.text
        expect(row_a).not_to_be_visible(timeout=15_000)

        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Mark as read", exact=True,
        ).click()

        def _both_marked_read() -> bool | None:
            detail_a = api_client.get(f"/api/messages/{msg_a['id']}").json()
            detail_b = api_client.get(f"/api/messages/{msg_b['id']}").json()
            return True if detail_a["is_seen"] and detail_b["is_seen"] else None

        wait_for(
            _both_marked_read, timeout_s=20.0,
            description=(
                "both messages marked read -- including the one whose row "
                "had already left the list cache when the action ran"
            ),
        )
