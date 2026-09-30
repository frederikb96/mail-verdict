"""
Glacier storage from the browser: moving mail into it behind a
confirmation that names the count and says plainly that it leaves the
mail server for good, with no undo offered for it once confirmed; the
sidebar's own folder row for it, excluded from folder management, with
its own per-folder menu (mark all as read, but not empty folder); the
account form's own switch persisting and refusing to be turned off
while the glacier still holds something; and its presence in the search
scope picker.

Restoring a message needs an IMAP APPEND, which the pinned default
PostIMAP test image (tests/setup/images.py) does not support yet -- run
this module with MAIL_VERDICT_TEST_POSTIMAP_IMAGE pointed at a build
that does (ghcr.io/frederikb96/postimap:1.11.0 at the time of writing)
or the moves in TestGlacierMoveUi time out waiting for the glacier
folder's own count, since the manual glacier-in flow itself is gated on
the same capability check restoring needs.

Every move here is proven by the glacier folder's own count (GET
/accounts/{id}/folders, which reads visible_at correctly) rather than by
asking after a message's own identity: the move response, get_message,
get_thread and locate_message all now resolve a message glaciered this
way under the id the client held before the move, but nothing in this
module needs that id, so it is left unexercised here -- see
test_reading_pane_ui.py or the equivalent for coverage of opening a
glaciered message directly.

Empty folder is deliberately not offered on the glacier's own menu:
GET .../messages/selection, which the confirmation mints its count
from, does not yet resolve a glacier folder id and always answers a
count of zero, so the confirmation would misname the count and the
server's own mismatch guard would then refuse the actual delete for any
glacier genuinely holding something.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    create_account,
    drag_row_to_folder,
    folder,
    folder_button,
    mail_row,
    select_account,
    wait_for,
    wait_for_folder,
)

GLACIER_WARNING = "leaves the mail server for good"


@pytest.fixture(scope="module")
def ui_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    return create_account(api_client, "glacier-ui")


@pytest.fixture(scope="module")
def inbox_folder(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "INBOX")


@pytest.fixture(scope="module")
def glacier_enabled(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    """Turns the glacier on for ui_account once, through the API -- every
    test in this module needs it on, and only test_account_settings_ui
    below is actually about the switch itself."""
    resp = api_client.patch(f"/api/accounts/{ui_account['id']}", json={"glacier_enabled": True})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def glacier_folder(
    api_client: httpx.Client, ui_account: dict[str, Any], glacier_enabled: dict[str, Any],
) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "Glacier")


def _deliver_to_inbox(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account_id: str,
    recipient: str,
    inbox_folder_id: str,
    subject: str,
    message_id: str | None = None,
) -> dict[str, Any]:
    host, _imap_port, lmtp_port = dovecot_endpoint
    message = build_eml(
        sender="sender@example.com", recipient=recipient, subject=subject,
        message_id=message_id or f"<{uuid.uuid4()}@example.com>",
    )
    deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=recipient)

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account_id}/messages", params={"folder_id": inbox_folder_id},
        )
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_find, timeout_s=60.0, description=f"{subject!r} synced into INBOX")


def _glacier_total(api_client: httpx.Client, account_id: str) -> int:
    resp = api_client.get(f"/api/accounts/{account_id}/folders")
    assert resp.status_code == 200, resp.text
    glacier = next(f for f in resp.json() if f["kind"] == "glacier")
    return glacier["total_count"]


def _wait_glacier_total(
    api_client: httpx.Client, account_id: str, expected: int, timeout_s: float = 30.0,
) -> None:
    """Confirms a move landed by the glacier folder's own count. The move
    response reports the glacier row's own fresh id, and get_message,
    get_thread and locate_message all resolve it back from the original
    id too -- but this module never captures that id in the first place
    (its assertions are all about the confirmation and the move's effect
    on the two folders involved), so the count is what it waits on."""

    def _check() -> bool | None:
        return _glacier_total(api_client, account_id) == expected or None

    wait_for(
        _check, timeout_s=timeout_s,
        description=f"glacier folder for {account_id} reaches {expected} message(s)",
    )


class TestGlacierMoveUi:
    """Shares one account, its glacier switched on, across every test."""

    def test_move_picker_confirms_names_the_count_and_moves_it_with_no_undo(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        glacier_folder: dict[str, Any],
    ) -> None:
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Glacier via picker {uuid.uuid4()}",
        )
        before = _glacier_total(api_client, ui_account["id"])

        page.goto(app_server)
        select_account(page, ui_account)
        folder_button(page, inbox_folder["id"]).click()
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()

        page.get_by_role("button", name="Move to…").click()
        page.get_by_placeholder("Move to…").fill("Glacier")
        page.get_by_role("option", name="Glacier").click()

        dialog_text = page.get_by_text(GLACIER_WARNING, exact=False)
        expect(dialog_text).to_be_visible(timeout=5_000)
        expect(page.get_by_role("button", name="Move to Glacier")).to_be_visible()

        page.get_by_role("button", name="Move to Glacier").click()
        expect(row).not_to_be_visible(timeout=15_000)

        _wait_glacier_total(api_client, ui_account["id"], before + 1)

        # No undo offered: pressing the shortcut must not move it back --
        # the glacier count must still read the incremented value.
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1_000)
        assert _glacier_total(api_client, ui_account["id"]) == before + 1, (
            "Ctrl+Z appears to have reversed a glacier move -- it must not be undoable"
        )

    def test_dragging_a_row_onto_the_glacier_confirms_and_moves_it(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        glacier_folder: dict[str, Any],
    ) -> None:
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Glacier via drag {uuid.uuid4()}",
        )
        before = _glacier_total(api_client, ui_account["id"])

        page.goto(app_server)
        select_account(page, ui_account)
        folder_button(page, inbox_folder["id"]).click()
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        drag_row_to_folder(page, row, folder(page, glacier_folder["id"]))

        dialog_text = page.get_by_text(GLACIER_WARNING, exact=False)
        expect(dialog_text).to_be_visible(timeout=5_000)
        page.get_by_role("button", name="Move to Glacier").click()

        expect(row).not_to_be_visible(timeout=15_000)
        _wait_glacier_total(api_client, ui_account["id"], before + 1)


class TestGlacierAccountSettingsUi:
    def test_enabling_persists_after_reload_and_refuses_to_disable_while_full(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account = create_account(api_client, "glacier-settings")
        inbox = wait_for_folder(api_client, str(account["id"]), "INBOX")

        page.goto(f"{app_server}/accounts")
        page.get_by_role("button", name=f"Expand {account['name']}").click()
        page.get_by_role("button", name=f"{account['name']} options").click()
        page.get_by_role("menuitem", name="Edit").click()
        page.get_by_label("Enable glacier storage").check()
        page.get_by_role("button", name="Update").click()
        expect(page.get_by_role("dialog", name="Edit Account")).to_have_count(0, timeout=10_000)

        # The card's own summary line reflects the write without a reload;
        # persistence itself is checked against the server directly below
        # rather than by racing this page's query cache through a reload.
        expect(page.get_by_text("On, manual only", exact=False)).to_be_visible(timeout=10_000)

        def _persisted() -> bool | None:
            detail = api_client.get(f"/api/accounts/{account['id']}").json()
            return detail["glacier_enabled"] or None

        wait_for(_persisted, description="glacier_enabled persisted on the server")

        # Glacier one message, through the API -- this test is about the
        # account form, not the move flow the other class already covers.
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, account["id"], account["email"], inbox["id"],
            f"Fill the glacier {uuid.uuid4()}",
        )
        glacier = wait_for_folder(api_client, str(account["id"]), "Glacier")
        resp = api_client.post(
            f"/api/messages/{target['id']}/action",
            json={"action": "move", "target_folder_id": glacier["id"]},
        )
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        _wait_glacier_total(api_client, account["id"], 1)

        page.get_by_role("button", name=f"{account['name']} options").click()
        page.get_by_role("menuitem", name="Edit").click()
        page.get_by_label("Enable glacier storage").uncheck()
        page.get_by_role("button", name="Update").click()

        refusal = page.get_by_text("the glacier still holds 1 message", exact=False)
        expect(refusal).to_be_visible(timeout=10_000)
        # The dialog must still be open -- the refused write did not close it.
        expect(page.get_by_label("Enable glacier storage")).to_be_visible()


class TestGlacierElsewhereInTheUi:
    def test_offered_in_the_search_folder_scope_picker(
        self,
        page: Page, app_server: str, ui_account: dict[str, Any], glacier_folder: dict[str, Any],
    ) -> None:
        page.goto(f"{app_server}/search")
        select_account(page, ui_account)
        page.get_by_role("button", name="folders", exact=False).first.click()
        # The picker is not scoped to the sidebar's own selected account --
        # it carries a search-only account scope of its own, defaulting to
        # every account, so two accounts on this run each contribute their
        # own folder literally named "Glacier"; .first only asks whether
        # it is offered at all.
        expect(page.get_by_text("Glacier", exact=True).first).to_be_visible(timeout=10_000)

    def test_excluded_from_manage_folders(
        self,
        page: Page, app_server: str, ui_account: dict[str, Any], glacier_folder: dict[str, Any],
    ) -> None:
        page.goto(app_server)
        select_account(page, ui_account)
        page.get_by_role("button", name="Manage folders").click()
        dialog = page.get_by_role("dialog", name="Manage folders")
        expect(dialog).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text("Glacier", exact=True)).to_have_count(0)

    def test_the_glacier_row_offers_mark_read_but_not_empty_folder(
        self,
        page: Page, app_server: str, ui_account: dict[str, Any], glacier_folder: dict[str, Any],
    ) -> None:
        """Mark all as read reaches the glacier the same way it reaches an
        ordinary folder now that bulk actions resolve a glacier scope.
        Empty folder stays out: the message-selection endpoint the
        confirmation would mint its count from does not yet resolve a
        glacier folder id (always answers a count of zero), so offering
        it here would show the wrong count and then be refused by the
        server's own count-mismatch guard for any glacier actually
        holding something."""
        page.goto(app_server)
        select_account(page, ui_account)
        folder(page, glacier_folder["id"]).hover()
        page.get_by_role("button", name="Glacier options").click()
        menu = page.get_by_role("menu")
        expect(menu.get_by_role("menuitem", name="Mark all as read")).to_be_visible(timeout=5_000)
        expect(menu.get_by_role("menuitem", name="Empty folder")).to_have_count(0)


class TestGlacierRowAndReadingPaneUi:
    """A glaciered message's own row and reading pane: what the row offers
    without a confirmation (star, read state -- nothing that restores),
    and that Archive/Move to trash on the message itself, opened in the
    reading pane, need one before they act."""

    def test_glacier_row_offers_no_spam_control_and_no_unconfirmed_restore(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        glacier_folder: dict[str, Any],
    ) -> None:
        """The red team's finding: mail-list-item.tsx knew nothing of
        is_glacier, so every glacier row offered the ordinary hover set
        including Move to Junk (which the server refuses outright) and
        Archive/Move to trash (which silently restored the message with
        no confirmation at all)."""
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Glacier row controls {uuid.uuid4()}",
        )
        resp = api_client.post(
            f"/api/messages/{target['id']}/action",
            json={"action": "move", "target_folder_id": glacier_folder["id"]},
        )
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        glacier_id = resp.json()["message_id"]

        page.goto(app_server)
        select_account(page, ui_account)
        folder_button(page, glacier_folder["id"]).click()
        row = mail_row(page, glacier_id)
        expect(row).to_be_visible(timeout=15_000)
        row.hover()

        # Not exact=True: a threaded row's per-message actions carry a
        # "(latest message in thread)" qualifier this module's account
        # (grouped by conversation) always adds, which an exact match on
        # the bare label misses -- passing whether the control is hidden
        # or just differently named, proving nothing either way.
        expect(row.get_by_role("button", name="Move to Junk", exact=False)).to_have_count(0)
        expect(row.get_by_role("button", name="Remove from Junk", exact=False)).to_have_count(0)
        expect(row.get_by_role("button", name="Archive", exact=False)).to_have_count(0)
        expect(row.get_by_role("button", name="Move to trash", exact=False)).to_have_count(0)
        # What it still offers, without any confirmation needed: star and
        # read state, both local to this database.
        expect(row.get_by_role("button", name="Star", exact=True)).to_be_visible()
        mark_read = row.get_by_role("button", name=re.compile("^Mark as (read|unread)$"))
        expect(mark_read).to_be_visible()

    def test_archiving_a_glaciered_message_from_the_reading_pane_needs_confirmation(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        glacier_folder: dict[str, Any],
    ) -> None:
        """Archive and Move to trash on an open glaciered message both
        restore it to the mail server -- the same red-team finding as
        the row's own controls, but in the reading pane's toolbar, which
        had no isGlacier check on either button at all. Confirmed here;
        the outcome (a synchronous, grace-period refusal, in this
        environment -- see below) is reported with restore-specific
        wording, never as a send.

        Trash rather than Archive: this module's own account has no
        Archive folder (CLAUDE.md's "The `ui` layer's account has no
        Archive folder" -- Archive would fail with "No archive folder
        found for this account" before the restore is even attempted)."""
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Glacier reading pane restore {uuid.uuid4()}",
        )
        resp = api_client.post(
            f"/api/messages/{target['id']}/action",
            json={"action": "move", "target_folder_id": glacier_folder["id"]},
        )
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        glacier_id = resp.json()["message_id"]
        before = _glacier_total(api_client, ui_account["id"])

        page.goto(app_server)
        select_account(page, ui_account)
        folder_button(page, glacier_folder["id"]).click()
        row = mail_row(page, glacier_id)
        expect(row).to_be_visible(timeout=15_000)
        row.click()

        page.get_by_role("button", name="Move to trash", exact=True).click()
        dialog = page.get_by_role("dialog", name="Move to trash?", exact=True)
        expect(dialog.get_by_text("goes back onto the mail server", exact=False)).to_be_visible(
            timeout=5_000,
        )
        # Not yet acted on -- the count must still read the pre-restore value.
        assert _glacier_total(api_client, ui_account["id"]) == before

        dialog.get_by_role("button", name="Move to Trash", exact=True).click()

        # start_restore itself refuses this, synchronously: glacier.confirm_grace_seconds
        # (600s, the config default) has not elapsed since the message was glaciered a moment
        # ago, and nothing short of the e2e layer's own direct grace_seconds=0 call bypasses
        # that floor -- proving the eventual success toast would mean waiting out the full
        # grace period, which the pg and e2e layers already cover. What this test can prove
        # quickly is that the refusal itself is honest: restore-specific wording, never a claim
        # of success, never "sent" or SMTP phrasing (kind="append" transmits nothing at all).
        refusal = page.get_by_text("still being removed from the mail server", exact=False)
        expect(refusal).to_be_visible(timeout=15_000)
        expect(page.get_by_text("Message restored", exact=False)).to_have_count(0)
        expect(page.get_by_text("Message sent", exact=True)).to_have_count(0)
        expect(page.get_by_text("SMTP", exact=False)).to_have_count(0)
        assert _glacier_total(api_client, ui_account["id"]) == before


class TestGlacierPartialBulkResultUi:
    def test_a_partial_bulk_move_shows_no_retry_for_the_irreversible_part(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        glacier_folder: dict[str, Any],
    ) -> None:
        """The red team's finding: a bulk move into the glacier that
        partially fails answered success: false, and the ledger's own
        request() folded any success: false straight into a plain
        refusal -- discarding affected_count and skipped_ids entirely,
        so a partial success (messages that genuinely left the server)
        settled as "failed", offering Retry over an action that cannot
        safely be repeated."""
        shared_message_id = f"<{uuid.uuid4()}@example.com>"
        already_glaciered = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Partial bulk baseline {uuid.uuid4()}",
            message_id=shared_message_id,
        )
        resp = api_client.post(
            f"/api/messages/{already_glaciered['id']}/action",
            json={"action": "move", "target_folder_id": glacier_folder["id"]},
        )
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        before = _glacier_total(api_client, ui_account["id"])

        host, _imap_port, lmtp_port = dovecot_endpoint
        # A forged duplicate of the message just glaciered -- same
        # Message-ID (msg_key is the header alone when one exists), a
        # different body, so the identity guard refuses it rather than
        # treating it as the same message.
        forged = build_eml(
            sender="sender@example.com", recipient=ui_account["email"],
            subject=f"Forged duplicate {uuid.uuid4()}", message_id=shared_message_id,
            body="Not the same message at all.",
        )
        deliver_message(
            forged, host, lmtp_port, sender="sender@example.com", recipient=ui_account["email"],
        )
        genuine = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Genuinely new for bulk move {uuid.uuid4()}",
        )

        def _forged_synced() -> dict[str, Any] | None:
            listing = api_client.get(
                f"/api/accounts/{ui_account['id']}/messages",
                params={"folder_id": inbox_folder["id"]},
            )
            for m in listing.json()["messages"]:
                if m["subject"].startswith("Forged duplicate"):
                    return m
            return None

        forged_row = wait_for(_forged_synced, description="the forged duplicate synced into INBOX")

        page.goto(app_server)
        select_account(page, ui_account)
        folder_button(page, inbox_folder["id"]).click()
        forged_locator = mail_row(page, forged_row["id"])
        genuine_locator = mail_row(page, genuine["id"])
        expect(forged_locator).to_be_visible(timeout=15_000)
        expect(genuine_locator).to_be_visible(timeout=15_000)

        forged_locator.get_by_role("checkbox").click()
        genuine_locator.get_by_role("checkbox").click()

        page.get_by_role("button", name="Move to", exact=True).click()
        page.get_by_role("menuitem", name="Glacier", exact=True).click()
        # Not GLACIER_WARNING itself -- that constant is the singular
        # wording ("leaves"), and a two-message confirmation reads
        # "leave" (plural). The substring both share is this one.
        dialog_text = page.get_by_text("the mail server for good", exact=False)
        expect(dialog_text).to_be_visible(timeout=5_000)
        page.get_by_role("button", name="Move to Glacier", exact=True).click()

        def _one_more_landed() -> bool | None:
            return _glacier_total(api_client, ui_account["id"]) == before + 1 or None

        wait_for(_one_more_landed, description="exactly the genuine message to reach the glacier")

        # The genuine message left INBOX; the forged one, refused, stays.
        expect(genuine_locator).not_to_be_visible(timeout=15_000)
        expect(forged_locator).to_be_visible()

        # Never presented as a failure inviting a repeat of an
        # irreversible partial success.
        expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
