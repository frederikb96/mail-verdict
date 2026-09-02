"""
The UI layer's spine: what the browser does with state, not the state
reconciliation the e2e layer already owns. Every action here goes through an
actual control a person clicks (row hover, the dedicated row button, drag),
not the API directly, and every assertion reads either the rendered DOM or
the real account through api_client -- never a mock.

test_spam_on_an_already_junked_message_settles and
test_dragging_a_junk_row_onto_trash_moves_it describe correct behaviour for
two backend/UI defects fixed elsewhere: a same-folder move stranding
imap_uid with a spinner that never clears, and dnd-kit's default collision
detection dropping a row on the folder above the pointer rather than the
one under it. Both are expected to fail until that fix lands.
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
    drag_row_to_folder,
    folder,
    mail_row,
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
    host, _imap_port, lmtp_port = dovecot_endpoint
    message = build_eml(
        sender="sender@example.com", recipient=recipient, subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>",
    )
    deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=recipient)

    def _find() -> dict[str, Any] | None:
        for m in _list_folder(api_client, account_id, inbox_folder_id):
            if m["subject"] == subject:
                return m
        return None

    target = wait_for(_find, description=f"{subject!r} synced into INBOX")

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

    def test_bulk_undo_after_trash_restores_every_row(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        ui_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """The bulk toolbar's Trash button offers the same Undo, moving
        every affected message back -- the selection's own compensating-move
        path, not the single-row one above."""
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
        _open_folder(page, inbox_folder)
        rows = [mail_row(page, target["id"]) for target in targets]
        for row in rows:
            expect(row).to_be_visible(timeout=15_000)
        rows[0].hover()
        rows[0].get_by_role("checkbox").click()
        rows[1].get_by_role("checkbox").click()

        page.get_by_role("main").get_by_role("button", name="Trash", exact=True).click()
        for row in rows:
            expect(row).not_to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Undo", exact=True).click()

        for target in targets:
            expect(mail_row(page, target["id"])).to_be_visible(timeout=15_000)
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
        imap_uid=NULL row that spins forever. Expected to fail until the
        backend treats spam-on-a-Junk-message as already-satisfied."""
        already_junked = _deliver_and_move(
            api_client, dovecot_endpoint, ui_account["id"], ui_account["email"],
            inbox_folder["id"], junk_folder["id"], f"Already junk {uuid.uuid4()}",
        )

        page.goto(app_server)
        _open_folder(page, junk_folder)
        row = mail_row(page, already_junked["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.get_by_title("Spam").click()

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
        _open_folder(page, junk_folder)
        row = mail_row(page, to_drag["id"])
        expect(row).to_be_visible(timeout=15_000)

        drag_row_to_folder(page, row, folder(page, trash_folder["id"]))

        def _moved() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/messages/{to_drag['id']}").json()
            return detail if detail["folder_id"] == trash_folder["id"] else None

        wait_for(_moved, timeout_s=30.0, description="Dragged row lands in Trash")

    def test_compose_and_send_shows_a_toast_and_reaches_mailpit(
        self, page: Page, app_server: str, mailpit_http_url: str,
    ) -> None:
        subject = f"UI send {uuid.uuid4()}"

        page.goto(app_server)
        page.get_by_role("button", name="Compose").click()
        dialog = page.get_by_role("dialog", name="New Message")
        expect(dialog).to_be_visible()

        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_role("textbox", name="Write your message...").fill(
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
        page.get_by_role("button", name="Compose").click()
        dialog = page.get_by_role("dialog", name="New Message")
        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_role("textbox", name="Write your message...").fill(
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
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.click()
        back = page.get_by_role("button", name="Back")
        expect(back).to_be_visible(timeout=10_000)
        expect(row).not_to_be_visible()

        back.click()
        expect(row).to_be_visible(timeout=10_000)
        expect(back).not_to_be_visible()

    def test_manage_folders_offers_no_delete_for_special_use_folders(
        self,
        page: Page,
        app_server: str,
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


def _trigger_sync(api_client: httpx.Client, account_id: str) -> None:
    """Force an immediate sync -- a sent or drafted copy otherwise only
    reappears on that folder's next periodic sync."""
    resp = api_client.post(f"/api/accounts/{account_id}/sync")
    assert resp.status_code == 200, resp.text
