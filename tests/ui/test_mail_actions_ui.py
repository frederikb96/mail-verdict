"""
The UI layer's spine: what the browser does with state, not the state
reconciliation the e2e layer already owns. Every action here goes through an
actual control a person clicks (row hover, the dedicated row button, drag),
not the API directly, and every assertion reads either the rendered DOM or
the real account through api_client -- never a mock.

test_spam_on_an_already_junked_message_settles and
test_dragging_a_junk_row_onto_trash_moves_it describe correct behaviour for
two backend/UI defects: a same-folder move stranding imap_uid with a
spinner that never clears, and dnd-kit's default collision detection
dropping a row on the folder above the pointer rather than the one under
it. move_message()'s own fix for the former is in place and both tests now
pass. test_spam_on_an_already_junked_message_settles goes through the bulk
toolbar's Spam button rather than the row's own -- a Junk row's own Spam
control is replaced by Not spam once rescuing from Junk is possible, so the
toolbar button is the one still-current control that can send an
already-junked message through the spam action.

test_arriving_mail_holds_the_list_scroll_position and
TestPhoneLayoutUi.test_contacts_page_has_an_add_control described correct
behaviour this suite never asserted before, against real, then-unfixed
defects (a virtualized-list prepend that did not compensate scroll
position, and a mobile contacts layout with no control that could ever
open the contact editor). Both now pass.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.helpers import wait_for_dav_account_active, wait_for_dav_collection
from tests.setup.dav_helpers import create_addressbook, create_calendar, discover
from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    drag_row_to_folder,
    folder,
    mail_row,
    select_account,
    unique_email,
    wait_for,
    wait_for_account_active,
    wait_for_folder,
    wait_for_mailpit_message,
)

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
def ui_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """An active account, wired to Mailpit, with three INBOX messages -- the
    shared starting point every scenario in this module builds on."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("ui")

    for i in range(3):
        message = build_eml(
            sender="sender@example.com", recipient=email, subject=f"UI seed {i}",
            message_id=f"<ui-seed-{uuid.uuid4()}@example.com>",
        )
        deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=email)

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
def junk_folder(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "Junk")


@pytest.fixture(scope="module")
def trash_folder(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "Trash")


@pytest.fixture(scope="module")
def drafts_folder(api_client: httpx.Client, ui_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(ui_account["id"]), "Drafts")


def _list_folder(
    api_client: httpx.Client, account_id: str, folder_id: str,
) -> list[dict[str, Any]]:
    resp = api_client.get(
        f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _deliver_to_inbox(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account_id: str,
    recipient: str,
    inbox_folder_id: str,
    subject: str,
    **eml: Any,
) -> dict[str, Any]:
    """Deliver one message over LMTP and wait for it to sync into INBOX.

    Anything else build_eml() accepts -- a body, a content type -- passes
    straight through.
    """
    host, _imap_port, lmtp_port = dovecot_endpoint
    message = build_eml(
        sender="sender@example.com", recipient=recipient, subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>", **eml,
    )
    deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=recipient)

    def _find() -> dict[str, Any] | None:
        for m in _list_folder(api_client, account_id, inbox_folder_id):
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_find, description=f"{subject!r} synced into INBOX")


def _deliver_and_move(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account_id: str,
    recipient: str,
    inbox_folder_id: str,
    junk_folder_id: str,
    subject: str,
) -> dict[str, Any]:
    """Deliver one message to INBOX, then move it into Junk through the API
    and wait for a real imap_uid -- setup for the two same-folder/drag
    scenarios, which must start from a message genuinely synced into Junk."""
    target = _deliver_to_inbox(
        api_client, dovecot_endpoint, account_id, recipient, inbox_folder_id, subject,
    )

    resp = api_client.post(
        f"/api/messages/{target['id']}/action",
        json={"action": "move", "target_folder_id": junk_folder_id},
    )
    assert resp.status_code == 200, resp.text

    def _synced() -> dict[str, Any] | None:
        detail = api_client.get(f"/api/messages/{target['id']}").json()
        if detail["folder_id"] == junk_folder_id and not detail["pending_sync"]:
            return detail
        return None

    return wait_for(_synced, description=f"{subject!r} genuinely moved into Junk")


def _open_folder(page: Page, folder_row: dict[str, Any]) -> None:
    folder(page, folder_row["id"]).get_by_role("button").click()


def _badge_count(page: Page, folder_id: str) -> int:
    """The unread badge on a sidebar folder, or 0 when it isn't rendered at all."""
    text = folder(page, folder_id).inner_text()
    match = re.search(r"(\d+)\s*$", text)
    return int(match.group(1)) if match else 0


class TestMailActionsUi:
    """Shares one account and its three seeded INBOX messages."""

    def test_mark_read_from_the_row_button(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        target = next(
            m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
            if not m["is_seen"]
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        before = _badge_count(page, inbox_folder["id"])

        # The always-visible mark-read icon, not a click on the row itself --
        # selecting a row auto-marks it read via the reading pane, which
        # would conflate the two paths this test tells apart.
        row.get_by_title("Mark as read").click()

        expect(row.locator(".bg-blue-500")).to_have_count(0, timeout=10_000)
        wait_for(
            lambda: _badge_count(page, inbox_folder["id"]) == before - 1 or None,
            timeout_s=10.0, description=f"INBOX badge drops from {before} to {before - 1}",
        )

        detail = api_client.get(f"/api/messages/{target['id']}").json()
        assert detail["is_seen"] is True

    def test_undo_after_trash_restores_the_row(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Trash acts immediately, but its success toast's Undo moves the
        message straight back to the folder it came from."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"Undo me {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=ui_account["email"], subject=subject,
            message_id=f"<{uuid.uuid4()}@example.com>",
        )
        deliver_message(
            message, host, lmtp_port, sender="sender@example.com", recipient=ui_account["email"],
        )

        def _find() -> dict[str, Any] | None:
            for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"]):
                if m["subject"] == subject:
                    return m
            return None

        target = wait_for(_find, description=f"{subject!r} synced into INBOX")

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, inbox_folder)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.hover()
        row.get_by_title("Move to trash").click()
        expect(row).not_to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Undo", exact=True).click()

        expect(mail_row(page, target["id"])).to_be_visible(timeout=15_000)
        detail = api_client.get(f"/api/messages/{target['id']}").json()
        assert detail["folder_id"] == inbox_folder["id"]

    def test_bulk_star_flags_every_selected_row(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The bulk panel's own Star button, the one bulk action that
        applies unconditionally to every selected row rather than
        confirming first -- flagging isn't destructive."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subjects = [f"Bulk star {i} {uuid.uuid4()}" for i in range(2)]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        targets = wait_for(_find_all, description="both bulk-star messages synced into INBOX")
        assert not any(m["is_flagged"] for m in targets)

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, inbox_folder)
        rows = [mail_row(page, target["id"]) for target in targets]
        for row in rows:
            expect(row).to_be_visible(timeout=15_000)
        rows[0].hover()
        rows[0].get_by_role("checkbox").click()
        rows[1].get_by_role("checkbox").click()

        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Star", exact=True,
        ).click()

        def _both_flagged() -> bool | None:
            details = [api_client.get(f"/api/messages/{t['id']}").json() for t in targets]
            return True if all(d["is_flagged"] for d in details) else None

        wait_for(_both_flagged, timeout_s=15.0, description="both messages flagged")

    def test_bulk_undo_after_trash_restores_every_row(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The bulk panel's Move to trash button offers the same Undo,
        moving every affected message back -- the selection's own
        compensating-move path, not the single-row one above."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subjects = [f"Bulk undo {i} {uuid.uuid4()}" for i in range(2)]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        targets = wait_for(_find_all, description="both bulk-undo messages synced into INBOX")

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, inbox_folder)
        rows = [mail_row(page, target["id"]) for target in targets]
        for row in rows:
            expect(row).to_be_visible(timeout=15_000)
        rows[0].hover()
        rows[0].get_by_role("checkbox").click()
        rows[1].get_by_role("checkbox").click()

        # Scoped to the bulk panel -- its Move to trash button now shares
        # its accessible name with every row's own, so "main" alone would
        # be a strict-mode violation across both.
        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Move to trash", exact=True,
        ).click()
        for row in rows:
            expect(row).not_to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Undo", exact=True).click()

        for target in targets:
            expect(mail_row(page, target["id"])).to_be_visible(timeout=15_000)
            detail = api_client.get(f"/api/messages/{target['id']}").json()
            assert detail["folder_id"] == inbox_folder["id"]

    def test_bulk_archive_with_no_archive_folder_shows_the_failure(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The bulk endpoint answers 200 with success: false when this
        account has no Archive folder -- the regression this guards is the
        toolbar reading only the HTTP status and showing a success/Undo
        toast for a request that moved nothing. Neither row should move,
        and the failure should be the one thing on screen."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subjects = [f"Bulk archive fail {i} {uuid.uuid4()}" for i in range(2)]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        targets = wait_for(_find_all, description="both bulk-archive messages synced into INBOX")

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, inbox_folder)
        rows = [mail_row(page, target["id"]) for target in targets]
        for row in rows:
            expect(row).to_be_visible(timeout=15_000)
        rows[0].hover()
        rows[0].get_by_role("checkbox").click()
        rows[1].get_by_role("checkbox").click()

        # "Archive" collides with every row's own hover-revealed Archive
        # button (same accessible name) -- scoped to the bulk panel, same
        # reasoning as the Move to trash button above.
        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Archive", exact=True,
        ).click()

        expect(
            page.get_by_text(
                "Could not archive: No archive folder found for this account", exact=True,
            )
        ).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name="Undo", exact=True)).to_have_count(0)
        for row in rows:
            expect(row).to_be_visible()
        for target in targets:
            detail = api_client.get(f"/api/messages/{target['id']}").json()
            assert detail["folder_id"] == inbox_folder["id"]

    def test_spam_on_an_already_junked_message_settles(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        junk_folder: dict[str, Any],
    ) -> None:
        """A same-folder move must be a no-op success, not a stranded
        imap_uid=NULL row that spins forever. A single row's own control
        is Remove from Junk once it sits in Junk, not Move to Junk -- so
        this reaches the spam action through the bulk panel instead, over
        two selected rows, which always sends it regardless of where the
        rows currently are."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subjects = [f"Already junk {uuid.uuid4()}", f"Also junk {uuid.uuid4()}"]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _find_both() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        targets = wait_for(
            _find_both, timeout_s=45.0, description="both already-junk messages synced into INBOX",
        )
        for target in targets:
            resp = api_client.post(
                f"/api/messages/{target['id']}/action",
                json={"action": "move", "target_folder_id": junk_folder["id"]},
            )
            assert resp.status_code == 200, resp.text

        def _both_settled_in_junk() -> list[dict[str, Any]] | None:
            details = [api_client.get(f"/api/messages/{t['id']}").json() for t in targets]
            if all(d["folder_id"] == junk_folder["id"] and not d["pending_sync"] for d in details):
                return details
            return None

        already_junked, also_junked = wait_for(
            _both_settled_in_junk, timeout_s=45.0,
            description="both messages genuinely moved into Junk",
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, junk_folder)
        row = mail_row(page, already_junked["id"])
        other_row = mail_row(page, also_junked["id"])
        expect(row).to_be_visible(timeout=15_000)
        expect(other_row).to_be_visible(timeout=15_000)

        row.hover()
        row.get_by_role("checkbox").click()
        other_row.get_by_role("checkbox").click()
        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Move to Junk", exact=True,
        ).click()

        expect(row.locator(".animate-spin")).to_have_count(0, timeout=10_000)
        expect(row).to_be_visible()

    def test_dragging_a_junk_row_onto_trash_moves_it(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        junk_folder: dict[str, Any],
        trash_folder: dict[str, Any],
    ) -> None:
        """dnd-kit's default collision detection must resolve to the folder
        under the pointer, not the row's neighbour. Expected to fail until
        the DndContext switches to pointer-based collision detection."""
        to_drag = _deliver_and_move(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], junk_folder["id"], f"Drag me {uuid.uuid4()}",
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, junk_folder)
        row = mail_row(page, to_drag["id"])
        expect(row).to_be_visible(timeout=15_000)

        drag_row_to_folder(page, row, folder(page, trash_folder["id"]))

        def _moved() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/messages/{to_drag['id']}").json()
            return detail if detail["folder_id"] == trash_folder["id"] else None

        wait_for(_moved, timeout_s=30.0, description="Dragged row lands in Trash")

    def test_compose_and_send_shows_a_toast_and_reaches_mailpit(
        self,
        page: Page,
        app_server: str,
        mailpit_http_url: str,
        ui_account: dict[str, Any],
    ) -> None:
        subject = f"UI send {uuid.uuid4()}"

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        page.get_by_role("button", name="Compose").click()
        dialog = page.get_by_role("dialog", name="New Message")
        expect(dialog).to_be_visible()

        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_test_id("mail-editor-body").fill(
            "Sent from the UI test.",
        )
        dialog.get_by_role("button", name="Send", exact=True).click()

        expect(page.get_by_text("Message queued for sending")).to_be_visible(timeout=10_000)
        wait_for_mailpit_message(mailpit_http_url, subject)

    def test_draft_save_reopen_send_leaves_drafts_empty(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        drafts_folder: dict[str, Any],
    ) -> None:
        subject = f"UI draft {uuid.uuid4()}"

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        page.get_by_role("button", name="Compose").click()
        dialog = page.get_by_role("dialog", name="New Message")
        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_test_id("mail-editor-body").fill(
            "A draft the UI test will reopen and send.",
        )
        dialog.get_by_role("button", name="Save draft").click()
        expect(page.get_by_text("Draft saved")).to_be_visible(timeout=10_000)

        _trigger_sync(api_client, ui_account["id"])
        draft = wait_for(
            lambda: next(
                (m for m in _list_folder(api_client, ui_account["id"], drafts_folder["id"])
                 if m["subject"] == subject), None,
            ),
            timeout_s=60.0, description=f"Draft {subject!r} synced into Drafts",
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        _open_folder(page, drafts_folder)
        mail_row(page, draft["id"]).click()
        expect(page.get_by_text("Editing draft")).to_be_visible(timeout=15_000)
        expect(page.get_by_role("textbox", name="Subject")).to_have_value(subject)

        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_text("Message queued for sending")).to_be_visible(timeout=10_000)

        _trigger_sync(api_client, ui_account["id"])

        def _drafts_empty_of_subject() -> bool | None:
            messages = _list_folder(api_client, ui_account["id"], drafts_folder["id"])
            return True if not any(m["subject"] == subject for m in messages) else None

        wait_for(_drafts_empty_of_subject, timeout_s=60.0, description="Drafts left empty")

    def test_arriving_mail_appears_without_a_reload(
        self,
        page: Page,
        app_server: str,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
    ) -> None:
        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        expect(
            page.get_by_role("button", name=re.compile("Connection status: Connected")),
        ).to_be_visible(timeout=15_000)

        subject = f"Arrived live {uuid.uuid4()}"
        host, _imap_port, lmtp_port = dovecot_endpoint
        message = build_eml(
            sender="sender@example.com", recipient=ui_account["email"], subject=subject,
            message_id=f"<{uuid.uuid4()}@example.com>",
        )
        deliver_message(
            message, host, lmtp_port, sender="sender@example.com", recipient=ui_account["email"],
        )

        expect(page.get_by_text(subject)).to_be_visible(timeout=45_000)

    def test_mobile_list_to_reading_pane_and_back(
        self, page: Page, app_server: str, api_client: httpx.Client,
        ui_account: dict[str, Any], inbox_folder: dict[str, Any],
    ) -> None:
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.click()
        back = page.get_by_role("button", name="Back")
        expect(back).to_be_visible(timeout=10_000)
        expect(row).not_to_be_visible()

        back.click()
        expect(row).to_be_visible(timeout=10_000)
        expect(back).not_to_be_visible()

    def test_folder_hover_menu_marks_every_message_read(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Hovering a folder replaces its unread badge with a three-dot
        control; its Mark all as read entry flips every unread message
        in the folder, not just whatever page happens to be loaded."""
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], f"Folder menu unread {uuid.uuid4()}",
        )
        assert target["is_seen"] is False

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        expect(mail_row(page, target["id"])).to_be_visible(timeout=15_000)

        inbox_row = folder(page, inbox_folder["id"])
        inbox_row.hover()
        inbox_row.get_by_role("button", name="INBOX options").click()
        page.get_by_role("menuitem", name="Mark all as read").click()

        def _target_read() -> bool | None:
            detail = api_client.get(f"/api/messages/{target['id']}").json()
            return True if detail["is_seen"] else None

        wait_for(
            _target_read, timeout_s=15.0,
            description=f"{target['subject']!r} marked read via the folder menu",
        )

    def test_folder_hover_menu_empties_a_folder_with_confirmation(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Emptying a folder destroys every message in it on the server
        with no undo, so the menu confirms first, naming the count -- and
        offers no Rename, which IMAP cannot express as a single-row
        update. Uses a folder created just for this test rather than a
        shared one, since emptying is irreversible."""
        resp = api_client.post(
            f"/api/accounts/{ui_account['id']}/folders",
            json={"name": f"Empty-me-{uuid.uuid4().hex[:8]}"},
        )
        assert resp.status_code == 201, resp.text
        custom_folder = wait_for_folder(
            api_client, str(ui_account["id"]), resp.json()["imap_name"],
        )

        host, _imap_port, lmtp_port = dovecot_endpoint
        subjects = [f"Empty test {i} {uuid.uuid4()}" for i in range(2)]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        targets = wait_for(
            _find_all, timeout_s=45.0, description="both empty-test messages synced into INBOX",
        )
        for target in targets:
            resp = api_client.post(
                f"/api/messages/{target['id']}/action",
                json={"action": "move", "target_folder_id": custom_folder["id"]},
            )
            assert resp.status_code == 200, resp.text

        def _both_settled() -> bool | None:
            details = [api_client.get(f"/api/messages/{t['id']}").json() for t in targets]
            settled = all(
                d["folder_id"] == custom_folder["id"] and not d["pending_sync"] for d in details
            )
            return True if settled else None

        wait_for(
            _both_settled, timeout_s=30.0,
            description="both messages genuinely moved into the custom folder",
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        custom_row = folder(page, custom_folder["id"])
        expect(custom_row).to_be_visible(timeout=15_000)

        custom_row.hover()
        custom_row.get_by_role(
            "button", name=re.compile(re.escape(custom_folder["imap_name"]) + r" options"),
        ).click()

        expect(page.get_by_role("menuitem", name="Rename")).to_have_count(0)
        page.get_by_role("menuitem", name="Empty folder").click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text("2 messages", exact=False)).to_be_visible()
        dialog.get_by_role("button", name="Empty folder", exact=True).click()

        target_ids = {t["id"] for t in targets}

        def _both_gone() -> bool | None:
            # expunge() is a soft delete (expunged_at, the row survives) --
            # GET .../messages/{id} doesn't filter on it, so absence from
            # the folder listing (which does) is the observable proof.
            remaining = {
                m["id"] for m in _list_folder(api_client, ui_account["id"], custom_folder["id"])
            }
            return True if not (target_ids & remaining) else None

        wait_for(_both_gone, timeout_s=20.0, description="both messages permanently deleted")

        # Clean up the folder itself -- it's now empty, and this account is
        # shared with test_manage_folders_offers_no_delete_for_special_use_folders,
        # which assumes every folder on it is special-use.
        resp = api_client.delete(
            f"/api/folders/{custom_folder['id']}", params={"confirm_message_count": 0},
        )
        assert resp.status_code == 204, resp.text

    def test_an_account_that_never_connected_shows_its_error_in_the_mail_view(
        self, page: Page, app_server: str, api_client: httpx.Client,
    ) -> None:
        """A never-connected account (`error`, `last_full_sync IS NULL`)
        has no folder for the auto-select effect to land on, so the mail
        view previously spun "Loading folders..." forever with nothing
        telling the reader why -- the same information the Accounts page
        already shows correctly. Reads the same predicate that page uses
        rather than a second one computed here."""
        email = unique_email("never-connects")
        resp = api_client.post(
            "/api/accounts",
            json={
                "name": email,
                "imap_host": "nonexistent-host-does-not-resolve.invalid",
                "imap_port": 993,
                "imap_user": email,
                "imap_password": "wrong",
            },
        )
        assert resp.status_code == 201, resp.text
        account = resp.json()
        account["email"] = email

        def _errored() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/accounts/{account['id']}").json()
            return detail if detail["state"] == "error" else None

        detail = wait_for(_errored, timeout_s=30.0, description="account settles into error")
        assert detail["state_error"], "expected a state_error message once erroring"

        page.goto(app_server)
        select_account(page, account)
        expect(page.get_by_text(detail["state_error"], exact=True)).to_be_visible(timeout=10_000)

    def test_create_folder_naming_an_existing_one_shows_the_conflict(
        self,
        page: Page,
        app_server: str,
        ui_account: dict[str, Any],
        junk_folder: dict[str, Any],
    ) -> None:
        """The server rejects a folder name already live on the account
        with 409 -- the dialog surfaces it as a toast rather than doing
        nothing visible and leaving only a console error."""
        page.goto(app_server)
        select_account(page, ui_account)
        page.get_by_role("button", name="Manage folders", exact=True).click()

        dialog = page.get_by_role("dialog", name="Manage folders")
        expect(dialog).to_be_visible(timeout=15_000)
        dialog.get_by_placeholder("New folder name").fill(junk_folder["imap_name"])
        dialog.get_by_role("button", name="Create", exact=True).click()

        expect(page.get_by_text("Could not create folder:", exact=False)).to_be_visible(
            timeout=10_000,
        )

    def test_manage_folders_offers_no_delete_for_special_use_folders(
        self,
        page: Page,
        app_server: str,
        ui_account: dict[str, Any],
        junk_folder: dict[str, Any],
        trash_folder: dict[str, Any],
        drafts_folder: dict[str, Any],
    ) -> None:
        """The regression this guards: Manage folders listed every folder
        with a Delete button, special-use ones included -- so Junk, Trash
        and Drafts, which a person should never destroy from here, offered
        exactly the same irreversible action as any folder they created
        themselves."""
        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        page.get_by_role("button", name="Manage folders", exact=True).click()

        dialog = page.get_by_role("dialog", name="Manage folders")
        expect(dialog).to_be_visible(timeout=15_000)

        for special_folder in (junk_folder, trash_folder, drafts_folder):
            label = special_folder["display_name"] or special_folder["imap_name"]
            expect(dialog.get_by_text(label, exact=True)).to_be_visible(timeout=10_000)

        # Every folder this account has is special-use (INBOX isn't listed
        # here at all, and none of the others were created by hand) -- so
        # the whole dialog offers no Delete button, not just the three
        # checked by name above.
        expect(dialog.get_by_role("button", name="Delete folder")).to_have_count(0)

    def test_arriving_mail_holds_the_list_scroll_position(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Nothing in this suite asserted that a message arriving over IDLE
        -- which is prepended above whatever the reader currently has in
        view, not appended below it -- leaves their own scroll position
        alone. A virtualized list re-rendered naively on every new row
        could as easily reset to the top or shift the row under the
        pointer; a batch of throwaway messages here is only to make the
        list tall enough to actually be scrolled."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        batch = f"scroll batch {uuid.uuid4()}"
        subjects = [f"{batch} {i:02d}" for i in range(40)]
        for subject in subjects:
            message = build_eml(
                sender="sender@example.com", recipient=ui_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=ui_account["email"],
            )

        def _batch_synced() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
                if m["subject"] in subjects
            ]
            return found if len(found) == len(subjects) else None

        wait_for(_batch_synced, timeout_s=30.0, description="Scroll-test batch synced into INBOX")

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        expect(page.locator('[data-testid="mail-row"]').first).to_be_visible(timeout=15_000)

        # Nothing is focused yet, so j/k move the list's own focus/scroll
        # rather than typing anywhere -- well past whatever the first
        # screenful already showed, and comfortably short of the batch's
        # own tail, where nearing the end triggers pagination's own
        # re-render and would confound this with a second cause.
        for _ in range(10):
            page.keyboard.press("j")
        page.wait_for_timeout(300)

        def _rendered_rows() -> list[dict[str, Any]]:
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=\"mail-row\"]'))"
                ".map((el) => ({id: el.getAttribute('data-mail-id'), "
                "top: el.getBoundingClientRect().top}))"
            )

        before = _rendered_rows()
        assert len(before) >= 3, (
            "expected the virtualized list to be showing only part of the batch, "
            f"not all of it at once: {before!r}"
        )
        anchor = before[len(before) // 2]

        before_unread = _badge_count(page, inbox_folder["id"])
        subject = f"Arrived above the fold {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=ui_account["email"], subject=subject,
            message_id=f"<{uuid.uuid4()}@example.com>",
        )
        deliver_message(
            message, host, lmtp_port, sender="sender@example.com", recipient=ui_account["email"],
        )

        # The arrival is proven by the live badge count, not by the new
        # row becoming visible -- it lands above the current scroll
        # position, so it staying off-screen is the point of this test,
        # not a sign the arrival was missed.
        after_unread = before_unread + 1
        wait_for(
            lambda: _badge_count(page, inbox_folder["id"]) == after_unread or None,
            timeout_s=45.0,
            description=f"INBOX badge rises from {before_unread} to {after_unread}",
        )

        after = {row["id"]: row["top"] for row in _rendered_rows()}
        assert anchor["id"] in after, "the row that was in view scrolled away or unmounted"
        assert abs(after[anchor["id"]] - anchor["top"]) < 2.0, (
            f"row {anchor['id']} moved from {anchor['top']} to {after[anchor['id']]} "
            "on the screen -- the list jumped when mail arrived above the viewport"
        )

    def test_row_controls_have_distinct_accessible_names(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A screen reader distinguishes the row's controls only if none
        of their accessible names is a substring of another -- the exact
        trap 'Spam' / 'Not spam' used to be."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()

        names = [b.get_attribute("aria-label") for b in row.get_by_role("button").all()]
        assert all(names), f"every row control needs an accessible name: {names!r}"
        assert len(names) == len(set(names)), f"duplicate accessible names: {names!r}"
        for name in names:
            others = [n for n in names if n != name]
            assert not any(name in other or other in name for other in others), (
                f"{name!r} is a substring of, or contains, another control's name in {names!r}"
            )

    def test_reading_pane_action_row_controls_have_distinct_accessible_names(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The consolidated action row keeps the two spam-adjacent
        controls tellable apart: Junk moves the message to the Junk
        folder, the verdict thumb corrects the model's classification --
        two different actions that must not collide under the same
        substring trap the row's own hover controls guard against."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()

        toolbar = page.get_by_role("toolbar", name="Message actions")
        expect(toolbar).to_be_visible(timeout=15_000)
        # Anchors (the download link) carry role="link", not "button" --
        # both element types are collected so the download control's own
        # name is checked alongside the rest.
        names = [
            el.get_attribute("aria-label") for el in toolbar.locator("button, a").all()
        ]
        # star, download, mark read/unread, archive, junk, trash -- the
        # verdict thumb is conditional on a verdict existing yet.
        assert len(names) >= 6, f"expected at least six always-present controls: {names!r}"
        assert all(names), f"every control needs an accessible name: {names!r}"
        assert len(names) == len(set(names)), f"duplicate accessible names: {names!r}"
        for name in names:
            others = [n for n in names if n != name]
            assert not any(name in other or other in name for other in others), (
                f"{name!r} is a substring of, or contains, another control's name in {names!r}"
            )

    def test_row_controls_are_reachable_by_keyboard(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Controls that only ever appear on hover are unusable without a
        mouse unless keyboard focus reveals them the same way."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        trash_button = row.get_by_title("Move to trash")
        assert trash_button.evaluate("el => getComputedStyle(el).opacity") == "0", (
            "expected the control hidden before focus, to prove focus is what reveals it"
        )

        row.focus()
        expect(trash_button).to_have_css("opacity", "1", timeout=5_000)

    def test_row_text_keeps_full_width_while_controls_float_over_it(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Hovering must not shrink the sender/subject text to make room
        for the row's controls -- they float over the row instead."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        subject_text = row.locator("span.truncate").first

        width_before = subject_text.evaluate("el => el.getBoundingClientRect().width")
        row.hover()
        expect(row.get_by_title("Archive")).to_have_css("opacity", "1", timeout=5_000)
        width_during_hover = subject_text.evaluate("el => el.getBoundingClientRect().width")

        assert width_during_hover == width_before, (
            f"row text narrowed from {width_before}px to {width_during_hover}px on hover -- "
            "controls should float over the row, not push its content aside"
        )

    def test_threaded_row_controls_name_their_scope(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A row action in threaded mode acts on the thread's latest
        message only -- pre-existing, and not something this lane
        changes -- so its controls say so rather than leaving the scope
        for the user to guess at."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        # "Group by conversation" defaults on, so this is already threaded.
        expect(page.get_by_role("switch", name="Group by conversation")).to_be_checked()
        expect(row.get_by_title("Archive (latest message in thread)")).to_have_count(1)
        expect(row.get_by_title("Move to trash (latest message in thread)")).to_have_count(1)

        page.get_by_role("switch", name="Group by conversation").click()
        expect(row.get_by_title("Archive", exact=True)).to_have_count(1)
        expect(row.get_by_title("Archive (latest message in thread)")).to_have_count(0)

    def test_selection_banner_offers_to_select_the_whole_folder(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The banner names the current count and offers to extend the
        selection to the whole folder -- Outlook's own pattern, which is
        what this is built to match. The folder here is small enough
        that everything loads in one page, so this goes through the
        always-available Select menu rather than the banner's own
        auto-surfaced offer, which only appears once the folder holds
        more than what is currently loaded."""
        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]
        folder_total = api_client.get(
            f"/api/accounts/{ui_account['id']}/folders",
        ).json()
        inbox_total = next(
            f["total_count"] for f in folder_total if f["id"] == inbox_folder["id"]
        )

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        page.get_by_role("switch", name="Group by conversation").click()

        row.hover()
        row.get_by_role("checkbox").click()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Select", exact=True).click()
        page.get_by_role("menuitem", name="Every message in this folder", exact=True).click()

        expect(page.get_by_text(f"{inbox_total} selected", exact=True)).to_be_visible(
            timeout=10_000,
        )

    def test_navigating_away_clears_the_selection_rather_than_leaving_it_actionable(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        junk_folder: dict[str, Any],
    ) -> None:
        """A selection is scoped to the list it was made in. Switching to a
        different folder must make the selection banner and the bulk
        toolbar disappear entirely -- not just stop showing them, but
        actually give up the selection -- rather than leave them up and
        actionable against whatever folder happens to be on screen when a
        button is finally pressed. Proves both halves the selection can be
        in: a folder-wide "select all" predicate, and a hand-picked pair of
        explicit rows."""
        subjects = [f"Scope guard {i} {uuid.uuid4()}" for i in range(2)]
        targets = [
            _deliver_to_inbox(
                api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
                inbox_folder["id"], subject,
            )
            for subject in subjects
        ]
        target_ids = {t["id"] for t in targets}

        page.goto(app_server)
        # A fresh load auto-selects whichever account sorts first by name
        # across the shared test database, not necessarily ui_account --
        # earlier modules in the same session have created accounts of
        # their own by the time this one runs.
        select_account(page, ui_account)
        first_row = mail_row(page, targets[0]["id"])
        second_row = mail_row(page, targets[1]["id"])
        expect(first_row).to_be_visible(timeout=15_000)
        page.get_by_role("switch", name="Group by conversation").click()

        # Predicate half: "select all" over the whole folder.
        first_row.hover()
        first_row.get_by_role("checkbox").click()
        page.get_by_role("button", name="Select", exact=True).click()
        page.get_by_role("menuitem", name="Every message in this folder", exact=True).click()
        expect(page.get_by_role("toolbar", name="Bulk actions")).to_be_visible(timeout=10_000)

        _open_folder(page, junk_folder)
        expect(page.get_by_role("toolbar", name="Bulk actions")).to_have_count(0, timeout=10_000)
        expect(page.get_by_role("toolbar", name="Selection")).to_have_count(0)

        _open_folder(page, inbox_folder)
        expect(first_row).to_be_visible(timeout=15_000)
        expect(first_row.get_by_role("checkbox")).not_to_be_checked()
        expect(page.get_by_role("toolbar", name="Selection")).to_have_count(0)
        remaining = {
            m["id"] for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
        }
        assert target_ids <= remaining, (
            "a stale predicate selection must not have been actionable in the "
            "folder navigated to -- both seed messages should still be in INBOX"
        )

        # Explicit-id half: two hand-picked rows, no predicate.
        first_row.hover()
        first_row.get_by_role("checkbox").click()
        second_row.hover()
        second_row.get_by_role("checkbox").click()
        expect(page.get_by_text("2 selected", exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_role("toolbar", name="Bulk actions")).to_be_visible(timeout=10_000)

        _open_folder(page, junk_folder)
        expect(page.get_by_role("toolbar", name="Bulk actions")).to_have_count(0, timeout=10_000)

        _open_folder(page, inbox_folder)
        expect(first_row).to_be_visible(timeout=15_000)
        expect(second_row).to_be_visible(timeout=15_000)
        expect(first_row.get_by_role("checkbox")).not_to_be_checked()
        expect(second_row.get_by_role("checkbox")).not_to_be_checked()
        remaining = {
            m["id"] for m in _list_folder(api_client, ui_account["id"], inbox_folder["id"])
        }
        assert target_ids <= remaining, (
            "a stale explicit-id selection must not have been actionable in "
            "the folder navigated to -- both seed messages should still be in INBOX"
        )

    def test_selection_does_not_survive_account_switch_calendar_roundtrip_or_threading_toggle(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The guard covers every shape where the list a selection was made
        against stops being the list on screen, not only a folder switch
        (covered above): switching accounts, navigating away to another
        view and back, and toggling threading -- each changes what the
        selection's ids even mean, or (for the view round-trip) leaves the
        reader unsure whether a page they haven't looked at in a while
        still reflects what they ticked."""
        second_email = unique_email("second-account")
        resp = api_client.post(
            "/api/accounts",
            json={
                "name": second_email,
                "imap_host": DOVECOT_ALIAS,
                "imap_port": DOVECOT_IMAP_PORT,
                "imap_user": second_email,
                "imap_password": DOVECOT_PASSWORD,
                "smtp_host": MAILPIT_ALIAS,
                "smtp_port": MAILPIT_SMTP_PORT,
                "smtp_user": second_email,
                "smtp_password": "unused",
            },
        )
        assert resp.status_code == 201, resp.text
        second_account = resp.json()
        second_account["email"] = second_email
        wait_for_account_active(api_client, second_account["id"])

        target = _list_folder(api_client, ui_account["id"], inbox_folder["id"])[0]

        page.goto(app_server)
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        page.get_by_role("switch", name="Group by conversation").click()

        # Account switch.
        row.hover()
        row.get_by_role("checkbox").click()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible(timeout=10_000)
        select_account(page, second_account)
        expect(page.get_by_role("toolbar", name="Selection")).to_have_count(0, timeout=10_000)

        select_account(page, ui_account)
        expect(row).to_be_visible(timeout=15_000)
        expect(row.get_by_role("checkbox")).not_to_be_checked()

        # Navigate away to another view (Calendar) and back.
        row.hover()
        row.get_by_role("checkbox").click()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible(timeout=10_000)
        page.get_by_role("link", name="Calendar", exact=True).click()
        page.wait_for_timeout(800)
        page.get_by_role("link", name="Mail", exact=True).click()
        expect(row).to_be_visible(timeout=15_000)
        expect(page.get_by_role("toolbar", name="Selection")).to_have_count(0, timeout=10_000)
        expect(row.get_by_role("checkbox")).not_to_be_checked()

        # Threading toggle.
        row.hover()
        row.get_by_role("checkbox").click()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible(timeout=10_000)
        page.get_by_role("switch", name="Group by conversation").click()
        expect(page.get_by_role("toolbar", name="Selection")).to_have_count(0, timeout=10_000)


def _rect(locator: Any) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None, "locator resolved to no element with a layout box"
    return box


def _overlaps(a: dict[str, float], b: dict[str, float]) -> bool:
    ax0, ax1 = a["x"], a["x"] + a["width"]
    ay0, ay1 = a["y"], a["y"] + a["height"]
    bx0, bx1 = b["x"], b["x"] + b["width"]
    by0, by1 = b["y"], b["y"] + b["height"]
    return max(ax0, bx0) < min(ax1, bx1) and max(ay0, by0) < min(ay1, by1)


class TestRowControlLayoutUi:
    """The row's persistent controls -- the star once a message is
    flagged, the read/unread icon, its timestamp -- sit in the row's own
    header line rather than in the floating group that only appears on
    hover. A message with no body renders no snippet, which is the row's
    shortest shape and the one with the least room to spare; a floating
    group sized or positioned to assume there is always a third line
    lands on top of the header line's own controls on exactly this shape.
    """

    def test_the_header_lines_own_controls_are_never_covered_by_the_hover_only_ones(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        subject = f"No snippet {uuid.uuid4()}"
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], subject, body="",
        )

        page.goto(app_server)
        select_account(page, ui_account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()
        # The hover-revealed group opacity-transitions in; give it time to
        # actually reach its resting position before reading its box.
        page.wait_for_timeout(300)

        read_icon = _rect(row.get_by_title("Mark as read"))
        timestamp = _rect(row.locator("span.text-xs.text-muted-foreground").first)
        # "Archive"/"Move to Junk" alone, not exact: threading is on by
        # default for this account's shared view state, which appends
        # " (latest message in thread)" to both titles.
        archive = _rect(row.get_by_title(re.compile(r"^Archive")))
        junk = _rect(row.get_by_title(re.compile(r"^Move to Junk")))

        assert not _overlaps(read_icon, archive)
        assert not _overlaps(read_icon, junk)
        assert not _overlaps(timestamp, archive)
        assert not _overlaps(timestamp, junk)


def _channels(colour: str) -> list[float]:
    """The three channels of a computed `rgb(...)` / `rgba(...)` value."""
    numbers = re.findall(r"[\d.]+", colour)
    assert len(numbers) >= 3, f"not a computed colour: {colour!r}"
    return [float(n) for n in numbers[:3]]


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance -- 0 for black, 1 for white."""
    def linear(raw: float) -> float:
        srgb = raw / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in _channels(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(one: str, other: str) -> float:
    """WCAG contrast between two computed colours, 1.0 (identical) to 21.0."""
    lighter, darker = sorted((_relative_luminance(one), _relative_luminance(other)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG AA for body text. Used here as the readable/unreadable line rather
# than as a compliance claim: the two defects below rendered at 1.27 and
# 1.68 respectively.
_READABLE_CONTRAST = 4.5

# The sender supplies a white background and leaves the text to the
# client. The text is then ours and the background is theirs.
_WHITE_BACKGROUND_HTML = (
    '<div style="background:#ffffff;padding:16px">'
    '<h1 style="color:#111111">Quarterly update</h1>'
    "<p>Body copy the sender left for the client to colour.</p>"
    "</div>"
)

# The mirror image: the sender supplies text colours -- greys chosen for a
# white page -- and no background at all. Now the text is theirs and the
# background is ours.
_DARK_TEXT_HTML = (
    '<div style="color:#4b5563;padding:16px">'
    '<h2 style="color:#1f2937">Tasks due today</h2>'
    "<p>Digest copy the sender coloured for a page it assumed was white.</p>"
    "</div>"
)

# One probe serves both shapes. Whichever of the two the message is, the
# text meets a background: the sender's own where they set one, ours where
# they did not.
_HTML_CANVAS_PROBE = """
(host) => {
    const root = host.shadowRoot;
    return {
        text: getComputedStyle(root.querySelector('p')).color,
        sender_background: getComputedStyle(root.querySelector('div')).backgroundColor,
        canvas: getComputedStyle(host).backgroundColor,
    };
}
"""

_PLAIN_TEXT_PROBE = """
(host) => {
    const pre = host.shadowRoot.querySelector('pre');
    return {
        text: getComputedStyle(pre).color,
        canvas: getComputedStyle(pre).backgroundColor,
    };
}
"""

_TRANSPARENT = "rgba(0, 0, 0, 0)"


def _computed_colours(
    browser: Browser,
    app_server: str,
    account: dict[str, Any],
    message_id: str,
    *,
    marker: str,
    probe: str,
) -> dict[str, str]:
    """Open one message in a dark-mode browser and read computed colours
    back out of its shadow root.

    `marker` is text the message body renders, waited on so the probe runs
    against content that is actually there rather than an empty root.
    """
    context = browser.new_context(color_scheme="dark")
    page = context.new_page()
    try:
        page.goto(app_server)
        select_account(page, account)
        row = mail_row(page, message_id)
        expect(row).to_be_visible(timeout=15_000)
        row.click()

        body_host = page.get_by_test_id("email-body")
        expect(body_host).to_be_visible(timeout=15_000)
        # Scoped to the message body: the row's own preview line carries
        # the same text and an unscoped match resolves to both. Playwright
        # pierces the open shadow root to reach the content itself.
        expect(body_host.get_by_text(marker)).to_be_visible(timeout=15_000)

        colours: dict[str, str] = body_host.evaluate(probe)
        return colours
    finally:
        context.close()


class TestMessageCanvasUi:
    """The canvas a message body renders on, which is not the application's.

    Every scenario here runs in a browser pinned to `prefers-color-scheme:
    dark` and reads computed colours out of the message's shadow root -- a
    light browser resolves the theme the other way and none of these
    defects exist there at all.
    """

    def test_dark_mode_leaves_html_mail_readable_on_a_sender_supplied_background(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The regression this guards: the shadow root's `:host` carried the
        application's dark-theme text colour, and `color` inherits straight
        through a shadow boundary. Mail that supplies its own white
        background and no text colour -- which is most marketing HTML --
        therefore took its background from the sender and its text from the
        theme: light grey on white, for every dark-mode reader."""
        subject = f"Quarterly canvas {uuid.uuid4()}"
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], subject,
            body=_WHITE_BACKGROUND_HTML, content_type="text/html; charset=utf-8",
        )

        colours = _computed_colours(
            browser, app_server, ui_account, target["id"],
            marker="Body copy the sender left", probe=_HTML_CANVAS_PROBE,
        )

        assert colours["sender_background"] == "rgb(255, 255, 255)", (
            "the sender's own white background did not survive to the browser, so this "
            f"proves nothing about text on it: {colours}"
        )
        contrast = _contrast_ratio(colours["text"], colours["sender_background"])
        assert contrast >= _READABLE_CONTRAST, (
            f"message text {colours['text']} on the sender's own "
            f"{colours['sender_background']} has contrast {contrast:.2f}"
        )

    def test_dark_mode_leaves_html_mail_readable_when_the_sender_colours_the_text(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The same defect from the other side, and the reason the fix is a
        canvas rather than a colour. Mail that colours its own text -- greys
        chosen for a white page -- and sets no background took its text from
        the sender and its background from the theme: dark grey on near
        black. Recolouring `:host`'s text alone would leave exactly this
        case broken while making the scenario above pass, so both shapes are
        asserted."""
        subject = f"Digest canvas {uuid.uuid4()}"
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], subject,
            body=_DARK_TEXT_HTML, content_type="text/html; charset=utf-8",
        )

        colours = _computed_colours(
            browser, app_server, ui_account, target["id"],
            marker="Digest copy the sender coloured", probe=_HTML_CANVAS_PROBE,
        )

        assert colours["sender_background"] == _TRANSPARENT, (
            "the message set a background of its own after all, so this proves nothing "
            f"about the canvas underneath it: {colours}"
        )
        assert colours["text"] == "rgb(75, 85, 99)", (
            "the sender's own text colour did not survive to the browser, so this proves "
            f"nothing about reading their text on our canvas: {colours}"
        )
        # A transparent canvas is not a light one: the text would then be
        # read against whatever lies beneath, and the ratio below would be
        # computed against a colour nobody ever sees.
        assert colours["canvas"] != _TRANSPARENT, (
            f"the message canvas is transparent rather than a colour of its own: {colours}"
        )
        contrast = _contrast_ratio(colours["text"], colours["canvas"])
        assert contrast >= _READABLE_CONTRAST, (
            f"the sender's own text {colours['text']} on the canvas we supply "
            f"{colours['canvas']} has contrast {contrast:.2f}"
        )

    def test_dark_mode_keeps_plain_text_mail_on_the_dark_canvas(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The other half of pinning the canvas: only mail carrying its own
        markup gets pinned. The wrapper around plain text is generated by
        the renderer itself and carries no colours of its own, so a
        dark-mode reader keeps dark-mode plain text."""
        subject = f"Plain canvas {uuid.uuid4()}"
        target = _deliver_to_inbox(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], subject,
            body="Plain body copy with no markup around it.",
        )

        colours = _computed_colours(
            browser, app_server, ui_account, target["id"],
            marker="Plain body copy with no markup", probe=_PLAIN_TEXT_PROBE,
        )

        assert _relative_luminance(colours["text"]) > _relative_luminance(colours["canvas"]), (
            f"plain text {colours['text']} on {colours['canvas']} is dark on light -- "
            "the generated wrapper should follow the application theme"
        )
        contrast = _contrast_ratio(colours["text"], colours["canvas"])
        assert contrast >= _READABLE_CONTRAST, (
            f"plain text {colours['text']} on {colours['canvas']} has contrast {contrast:.2f}"
        )


def _trigger_sync(api_client: httpx.Client, account_id: str) -> None:
    """Force an immediate sync -- a sent or drafted copy otherwise only
    reappears on that folder's next periodic sync."""
    resp = api_client.post(f"/api/accounts/{account_id}/sync")
    assert resp.status_code == 200, resp.text


@pytest.fixture(scope="module")
def phone_radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def phone_dav_principal(phone_radicale_base_url: str) -> str:
    username = f"phone-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, phone_radicale_base_url)
        create_calendar(client, principal, "personal", "Personal")
        create_addressbook(client, principal, "friends", "Friends")
    return username


@pytest.fixture(scope="module")
def phone_dav_account(api_client: httpx.Client, phone_dav_principal: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": phone_dav_principal,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def phone_calendar_collection(
    api_client: httpx.Client, phone_dav_account: dict[str, Any],
) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, phone_dav_account["id"], "Personal")


@pytest.fixture(scope="module")
def phone_addressbook(
    api_client: httpx.Client, phone_dav_account: dict[str, Any],
) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, phone_dav_account["id"], "Friends")


class TestPhoneLayoutUi:
    """Shares one DAV account holding both a calendar and an address book
    -- neither test below needs any data in them, only a genuinely synced
    account, since both regressions are about a control that is missing
    (or a tap that is mishandled) regardless of what a real mailbox or
    calendar would otherwise show."""

    def test_contacts_page_has_an_add_control(
        self, page: Page, app_server: str, phone_addressbook: dict[str, Any],
    ) -> None:
        """The regression this guards: ContactsPage's mobile branch
        rendered ContactList or ContactDetail and nothing else -- the
        ContactEditor sheet it mounted underneath had no control anywhere
        on the phone layout that could ever open it."""
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{app_server}/contacts")
        expect(page.get_by_placeholder("Search contacts")).to_be_visible(timeout=15_000)

        # useIsMobile() starts undefined -- !!undefined is false -- so the
        # very first render is always the desktop branch, which carries
        # its own "New contact" button; only the effect that follows
        # measures window.innerWidth and flips to the mobile branch.
        # Asserting on the add control before that flip would catch the
        # desktop layout's own button instead of the mobile one under
        # test -- wait for the desktop-only header to be gone first.
        expect(page.get_by_text("Contacts", exact=True)).to_have_count(0, timeout=10_000)

        expect(page.get_by_role("button", name=re.compile("new contact", re.I))).to_be_visible(
            timeout=10_000
        )

    def test_month_view_tapping_a_cell_opens_that_day(
        self, page: Page, app_server: str, phone_calendar_collection: dict[str, Any],
    ) -> None:
        """P2's own check: Month shows dots and opens the tapped day. The
        phone layout only swaps Week for Day (calendar-page.tsx); Month
        stays the same shared component and handler, so this proves the
        tap-to-open path itself, not anything phone-specific about it."""
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Month", exact=True).click()

        target = (datetime.now(timezone.utc) + timedelta(days=10)).date()
        cell = page.locator(f'[data-date="{target.isoformat()}"]')
        expect(cell).to_be_visible(timeout=15_000)
        cell.click()

        # date-fns "EEEE, MMMM d, yyyy" -- calendar-toolbar.tsx's own
        # format for the day view, distinct from month's "MMMM yyyy" and
        # week's range, so matching it also proves which view opened.
        expected_title = f"{target.strftime('%A, %B')} {target.day}, {target.year}"
        expect(page.get_by_test_id("calendar-toolbar-title")).to_have_text(
            expected_title, timeout=10_000,
        )
