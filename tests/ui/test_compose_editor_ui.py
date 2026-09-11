"""
The rich-text composer: pasted HTML keeps its formatting, long content
scrolls inside its own box rather than growing it without bound, a reply
embeds the original as a real quote rather than a lossy text dump, and
every composer surface can be closed -- with a prompt to save or discard
when there is unsaved work, the gap that most annoyed the owner.
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
    folder_button,
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
)


@pytest.fixture(scope="module")
def editor_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """An active account with one HTML message in INBOX to reply to."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("editor")

    message = build_eml(
        sender="sender@example.com", recipient=email, subject="Quoted original",
        message_id=f"<editor-{uuid.uuid4()}@example.com>",
        body="<h1>Original heading</h1><p>Original body text.</p>",
        content_type="text/html; charset=utf-8",
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
            "smtp_password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])
    account["email"] = email
    return account


@pytest.fixture(scope="module")
def inbox_folder(api_client: httpx.Client, editor_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(editor_account["id"]), "INBOX")


@pytest.fixture(scope="module")
def original_message(
    api_client: httpx.Client, editor_account: dict[str, Any], inbox_folder: dict[str, Any],
) -> dict[str, Any]:
    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{editor_account['id']}/messages",
            params={"folder_id": inbox_folder["id"]},
        )
        return next((m for m in resp.json()["messages"] if m["subject"] == "Quoted original"), None)

    return wait_for(_find, description="Original message synced into INBOX")


@pytest.fixture(scope="module")
def drafts_folder(api_client: httpx.Client, editor_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(editor_account["id"]), "Drafts")


@pytest.fixture(scope="module")
def plain_text_message(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
    editor_account: dict[str, Any], inbox_folder: dict[str, Any],
) -> dict[str, Any]:
    """A distinct, plain-text-only original -- so the quote this test
    reconstructs from a reopened draft's body_text has an unambiguous,
    known plain-text form to check against, rather than whatever an
    HTML-only message's own text alternative happens to be."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    subject = "Plain text original"
    message = build_eml(
        sender="sender@example.com", recipient=editor_account["email"], subject=subject,
        message_id=f"<editor-plain-{uuid.uuid4()}@example.com>",
        body="Original plain body line.",
    )
    deliver_message(
        message, host, lmtp_port, sender="sender@example.com", recipient=editor_account["email"],
    )

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{editor_account['id']}/messages",
            params={"folder_id": inbox_folder["id"]},
        )
        return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)

    return wait_for(_find, description=f"{subject!r} synced into INBOX")


def _trigger_sync(api_client: httpx.Client, account_id: str) -> None:
    """Force an immediate sync -- a drafted or sent copy otherwise only
    reappears on that folder's next periodic sync."""
    resp = api_client.post(f"/api/accounts/{account_id}/sync")
    assert resp.status_code == 200, resp.text


def _list_folder(
    api_client: httpx.Client, account_id: str, folder_id: str,
) -> list[dict[str, Any]]:
    resp = api_client.get(
        f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _open_folder(page: Page, folder_row: dict[str, Any]) -> None:
    folder_button(page, folder_row["id"]).click()


def _open_thread(
    page: Page, app_server: str, account: dict[str, Any], message: dict[str, Any],
) -> None:
    page.goto(app_server)
    select_account(page, account)
    page.locator(f'[data-testid="mail-row"][data-mail-id="{message["id"]}"]').click()


def _dispatch_paste(locator, html: str, text: str) -> None:
    """Simulate a clipboard paste offering both flavours -- the same event
    shape a real browser paste dispatches, so ProseMirror's own clipboard
    handling (not a mock of it) is what runs."""
    locator.evaluate(
        """(el, { html, text }) => {
            const dt = new DataTransfer();
            dt.setData('text/html', html);
            dt.setData('text/plain', text);
            const event = new ClipboardEvent('paste', {
                clipboardData: dt, bubbles: true, cancelable: true,
            });
            el.dispatchEvent(event);
        }""",
        {"html": html, "text": text},
    )


class TestPasteAndScroll:
    def test_pasted_html_keeps_its_formatting(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """The reported failure: rich content pasted from a note app
        arrived as raw HTML source, literally, as text. Offering the
        text/html clipboard flavour is what a source that also offers
        text/plain HTML source fails to do -- this is the case the editor
        fixes outright, the paste event's own text/html flavour winning."""
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        body = dialog.get_by_test_id("mail-editor-body")
        body.click()
        _dispatch_paste(body, "<p>plain <strong>bold</strong> text</p>", "plain bold text")

        expect(body.locator("strong")).to_have_text("bold")
        # Never as literal, visible source -- the exact failure reported.
        expect(body).not_to_contain_text("<strong>")

    def test_a_pasted_table_stays_a_table(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """A table pasted from a web page keeps its own structure -- cells
        in a row, not one run of text with the cell boundaries lost."""
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        body = dialog.get_by_test_id("mail-editor-body")
        body.click()
        _dispatch_paste(
            body,
            "<table><tr><td>cell A</td><td>cell B</td></tr></table>",
            "cell A\tcell B",
        )

        expect(body.locator("table")).to_have_count(1)
        cells = body.locator("td")
        expect(cells).to_have_count(2)
        expect(cells.nth(0)).to_have_text("cell A")
        expect(cells.nth(1)).to_have_text("cell B")

    def test_a_pasted_checklist_keeps_its_checkboxes(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """Every renderer outside this application writes a checklist as a
        list item with a checkbox in front of the text, which is what the
        clipboard carries when one is copied from a web page. Without a
        parse rule for that shape the ticks are dropped and the paste lands
        as an ordinary bullet list -- indistinguishable from a checklist
        that was never a checklist."""
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        body = dialog.get_by_test_id("mail-editor-body")
        body.click()
        _dispatch_paste(
            body,
            "<ul>"
            '<li><input type="checkbox" disabled checked> Bump the version</li>'
            '<li><input type="checkbox" disabled> Push the tag</li>'
            "</ul>",
            "Bump the version\nPush the tag",
        )

        items = body.locator('li[data-checked]')
        expect(items).to_have_count(2)
        assert items.nth(0).get_attribute("data-checked") == "true"
        assert items.nth(1).get_attribute("data-checked") == "false"

    def test_long_content_scrolls_inside_the_composer_rather_than_growing_it(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        body = dialog.get_by_test_id("mail-editor-body")
        body.click()
        long_text = "\n\n".join(f"Paragraph {i} of a very long message." for i in range(80))
        _dispatch_paste(body, "", long_text)

        scroller = dialog.locator('[data-testid="mail-editor-scroll"]')
        overflowing = scroller.evaluate("(el) => el.scrollHeight > el.clientHeight")
        assert overflowing, "the editor's content did not exceed its own box -- inconclusive"

        # The dialog itself stays within the viewport -- it is the inner
        # box that scrolls, not the page around it.
        dialog_box = dialog.bounding_box()
        viewport = page.viewport_size
        assert dialog_box is not None and viewport is not None
        assert dialog_box["height"] <= viewport["height"]

    def test_a_plain_text_paste_over_a_selected_word_stays_in_the_paragraph(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """A grammar-checker extension applies its correction by selecting
        the flagged word in the DOM and dispatching a synthetic paste event
        carrying only the replacement as plain text -- the same shape an
        ordinary paste over a selection takes. Replacing the selected range
        with a paragraph-shaped node instead of inline content splits the
        paragraph in two and strands the replacement between the halves."""
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        body = dialog.get_by_test_id("mail-editor-body")
        body.click()
        page.keyboard.type("This is a msitake in the sentence.")

        body.evaluate(
            """(el) => {
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const node = walker.nextNode();
                const offset = node.textContent.indexOf('msitake');
                const range = document.createRange();
                range.setStart(node, offset);
                range.setEnd(node, offset + 'msitake'.length);
                const selection = node.ownerDocument.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
            }""",
        )
        _dispatch_paste(body, "", "mistake")

        expect(body.locator("p")).to_have_count(1)
        expect(body).to_have_text("This is a mistake in the sentence.")


class TestRecipientFieldAccessibleName:
    def test_the_to_field_keeps_its_accessible_name_once_a_chip_exists(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """The visible placeholder disappears once there is a chip beside
        it -- correct, it would read oddly otherwise -- but the field's
        accessible name has to survive that. A locator by role and name is
        exactly what a screen reader relies on too."""
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        to_field = dialog.get_by_role("combobox", name="To", exact=True)
        to_field.click()
        to_field.fill("chip-recipient@example.com")
        page.keyboard.press("Enter")

        expect(dialog.get_by_text("chip-recipient@example.com")).to_be_visible(timeout=10_000)
        expect(dialog.get_by_role("combobox", name="To", exact=True)).to_be_visible(timeout=10_000)


class TestReplyQuoting:
    def test_reply_embeds_the_original_as_a_collapsible_quote(
        self,
        page: Page,
        app_server: str,
        editor_account: dict[str, Any],
        original_message: dict[str, Any],
    ) -> None:
        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()

        attribution = page.locator(".quoted-message-attribution")
        expect(attribution).to_be_visible(timeout=10_000)
        expect(attribution).to_contain_text("wrote:")

        toggle = page.locator(".quoted-message-toggle")
        expect(toggle).to_have_text("Show quoted text")
        toggle.click()
        expect(toggle).to_have_text("Hide quoted text")

        host = page.locator('[data-testid="quoted-message-shadow-host"]')
        quoted_heading = host.evaluate(
            "(el) => el.shadowRoot.querySelector('h1')?.textContent ?? ''",
        )
        assert quoted_heading == "Original heading"

    def test_editing_a_replys_subject_keeps_threading_intact(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        mailpit_http_url: str,
        editor_account: dict[str, Any],
        original_message: dict[str, Any],
    ) -> None:
        """The subject a reply starts with is derived, not fixed -- typing
        over it does not touch the threading headers, which come from the
        original message's id rather than from its subject line."""
        custom_subject = f"Something else entirely {uuid.uuid4()}"

        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()

        subject_field = page.get_by_role("textbox", name="Subject", exact=True)
        expect(subject_field).to_be_visible(timeout=10_000)
        subject_field.fill(custom_subject)

        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible(timeout=10_000)

        wait_for_mailpit_message(mailpit_http_url, custom_subject)

        _trigger_sync(api_client, editor_account["id"])
        sent_folder = wait_for_folder(api_client, str(editor_account["id"]), "Sent")
        sent = wait_for(
            lambda: next(
                (m for m in _list_folder(api_client, editor_account["id"], sent_folder["id"])
                 if m["subject"] == custom_subject),
                None,
            ),
            timeout_s=60.0, description=f"Reply {custom_subject!r} synced into Sent",
        )
        assert sent["thread_id"] == original_message["thread_id"]


class TestComposeRecoveryBuffer:
    def test_typing_into_a_reply_then_reloading_offers_it_back(
        self,
        page: Page,
        app_server: str,
        editor_account: dict[str, Any],
        original_message: dict[str, Any],
    ) -> None:
        """There is no server-side draft autosave -- see compose-form.tsx
        for why -- so a crash, reload or closed tab relies entirely on a
        local recovery buffer instead. Typing, then reloading, must offer
        the text back rather than silently losing it."""
        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("Recovered after a reload.")
        expect(body).to_contain_text("Recovered after a reload.")

        # The recovery buffer samples on an interval rather than on every
        # keystroke -- give it time to actually persist before reloading.
        page.wait_for_timeout(1500)

        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()

        expect(
            page.get_by_text("Recovered unsaved text from an earlier session.")
        ).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="Restore", exact=True).click()

        expect(page.get_by_test_id("mail-editor-body")).to_contain_text(
            "Recovered after a reload.",
        )


class TestReplyQuoteDoesNotLeakImages:
    """A message from a sender who is not allowlisted shows no images when
    read -- replying to it must not be a stronger signal than opening it,
    the same rule collapsing the quote alone cannot enforce (display:none
    does not stop an <img> from loading)."""

    def test_replying_to_an_unallowlisted_sender_issues_no_image_request(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"UI reply image privacy test {uuid.uuid4()}"
        message = build_eml(
            sender="Untrusted Sender <untrusted-reply-sender@example.com>",
            recipient=editor_account["email"], subject=subject,
            message_id=f"<reply-image-{uuid.uuid4()}@example.com>",
            body='<p>Hello</p><img src="https://tracker.invalid/pixel.gif">',
            content_type="text/html; charset=utf-8",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="untrusted-reply-sender@example.com", recipient=editor_account["email"],
        )

        def _find() -> dict[str, Any] | None:
            resp = api_client.get(
                f"/api/accounts/{editor_account['id']}/messages",
                params={"folder_id": inbox_folder["id"]},
            )
            return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)

        target = wait_for(_find, description=f"{subject!r} synced into INBOX")

        third_party_requests: list[str] = []

        def _record(req: object) -> None:
            url = req.url  # type: ignore[attr-defined]
            if "tracker.invalid" in url:
                third_party_requests.append(url)

        page.on("request", _record)

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, target["id"]).click()
        expect(page.locator('[data-testid="email-body"]')).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="Reply", exact=True).click()
        expect(page.locator(".quoted-message-attribution")).to_be_visible(timeout=10_000)
        toggle = page.locator(".quoted-message-toggle")
        toggle.click()
        expect(toggle).to_have_text("Hide quoted text")

        host_shadow = page.locator('[data-testid="quoted-message-shadow-host"]')
        image_count = host_shadow.evaluate(
            "(el) => el.shadowRoot.querySelectorAll('img').length",
        )
        assert image_count == 1, "the quoted image itself must survive, only its fetch is blocked"

        assert third_party_requests == [], (
            f"quoting an unallowlisted sender fetched: {third_party_requests}"
        )


class TestDraftReopenPreservesTheQuote:
    """A reply saved as a draft before anything was typed, reopened and
    sent without typing anything either -- the shape that previously lost
    the plain-text quote on reopen, and could fail outright on send since
    the HTML part still carried one and the text part did not."""

    def test_reopening_and_sending_an_untouched_reply_draft_keeps_the_quote(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        mailpit_http_url: str,
        editor_account: dict[str, Any],
        drafts_folder: dict[str, Any],
        plain_text_message: dict[str, Any],
    ) -> None:
        subject = "Re: Plain text original"

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, plain_text_message["id"]).click()
        page.get_by_role("button", name="Reply", exact=True).click()
        expect(page.locator(".quoted-message-attribution")).to_be_visible(timeout=10_000)

        page.get_by_role("button", name="Save draft", exact=True).click()
        expect(page.get_by_text("Draft saved")).to_be_visible(timeout=10_000)

        _trigger_sync(api_client, editor_account["id"])
        draft = wait_for(
            lambda: next(
                (m for m in _list_folder(api_client, editor_account["id"], drafts_folder["id"])
                 if m["subject"] == subject), None,
            ),
            timeout_s=60.0, description=f"Draft {subject!r} synced into Drafts",
        )

        page.goto(app_server)
        select_account(page, editor_account)
        _open_folder(page, drafts_folder)
        mail_row(page, draft["id"]).click()
        expect(page.get_by_text("Editing draft")).to_be_visible(timeout=15_000)
        expect(page.locator(".quoted-message-attribution")).to_be_visible(timeout=10_000)

        # Nothing typed -- the exact shape that previously 400'd, since the
        # HTML part still carried the quote (isEmpty() is false, an atom
        # node) while the reconstructed plain-text part came back empty.
        page.get_by_role("button", name="Send", exact=True).click()
        # A staged send reports itself through the undo banner, not a toast.
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible(timeout=10_000)

        mailpit_message = wait_for_mailpit_message(mailpit_http_url, subject)
        raw = httpx.get(
            f"{mailpit_http_url}/api/v1/message/{mailpit_message['ID']}/raw", timeout=10.0,
        )
        assert raw.status_code == 200, raw.text
        assert "> Original plain body line." in raw.text, (
            "expected the plain-text quote to survive the reopened draft; "
            f"raw source:\n{raw.text}"
        )


def _reopen_a_saved_reply_draft(
    page: Page,
    app_server: str,
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    editor_account: dict[str, Any],
    inbox_folder: dict[str, Any],
    drafts_folder: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Deliver a fresh original, reply to it, save the reply as a draft,
    wait for the draft to land in Drafts, and reopen it in the draft
    editor. Returns the draft's own message row -- its subject is unique to
    this call, so counting anything by it counts only this test's own."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    original_subject = f"UI {label} original {uuid.uuid4()}"
    draft_subject = f"Re: {original_subject}"
    message = build_eml(
        sender="sender@example.com", recipient=editor_account["email"],
        subject=original_subject, message_id=f"<{uuid.uuid4()}@example.com>",
        body=f"Body for the {label} test.",
    )
    deliver_message(
        message, host, lmtp_port,
        sender="sender@example.com", recipient=editor_account["email"],
    )

    def _find_original() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{editor_account['id']}/messages",
            params={"folder_id": inbox_folder["id"]},
        )
        return next(
            (m for m in resp.json()["messages"] if m["subject"] == original_subject), None,
        )

    original = wait_for(_find_original, description=f"{original_subject!r} synced into INBOX")

    page.goto(app_server)
    select_account(page, editor_account)
    mail_row(page, original["id"]).click()
    page.get_by_role("button", name="Reply", exact=True).click()
    body = page.get_by_test_id("mail-editor-body")
    body.click()
    body.type("A reply, saved as a draft.")
    expect(body).to_contain_text("A reply, saved as a draft.")

    page.get_by_role("button", name="Save draft", exact=True).click()
    expect(page.get_by_text("Draft saved")).to_be_visible(timeout=10_000)

    _trigger_sync(api_client, editor_account["id"])
    draft = wait_for(
        lambda: next(
            (m for m in _list_folder(api_client, editor_account["id"], drafts_folder["id"])
             if m["subject"] == draft_subject), None,
        ),
        timeout_s=60.0, description=f"Draft {draft_subject!r} synced into Drafts",
    )

    page.goto(app_server)
    select_account(page, editor_account)
    _open_folder(page, drafts_folder)
    mail_row(page, draft["id"]).click()
    expect(page.get_by_text("Editing draft")).to_be_visible(timeout=15_000)
    return draft


def _sends_for(
    api_client: httpx.Client, account_id: str, subject: str,
) -> list[dict[str, Any]]:
    """Every send carrying this subject, still inside its undo window or
    already handed on -- GET /api/outbox lists both."""
    resp = api_client.get("/api/outbox", params={"account_id": account_id})
    assert resp.status_code == 200, resp.text
    return [row for row in resp.json() if row["kind"] == "send" and row["subject"] == subject]


# Long enough for a send staged at the moment of pressing to leave its undo
# window (settings.outbox.undo_send_seconds, 5 s by default) and reach the
# outbox -- so a second send, had one been staged, would be counted too.
_PAST_THE_UNDO_WINDOW_MS = 8_000


class TestSendingAReopenedDraft:
    """Sending from a reopened draft used to raise the unsaved-changes
    prompt the moment the send succeeded -- the editor was still dirty when
    it closed itself, so its own guard read the close as navigating away --
    and dismissing that prompt left a composer whose Send worked a second
    time. That is how one message reached its recipient twice."""

    def test_sending_a_reopened_draft_sends_once_asks_nothing_and_removes_the_draft(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        drafts_folder: dict[str, Any],
    ) -> None:
        draft = _reopen_a_saved_reply_draft(
            page, app_server, api_client, dovecot_endpoint,
            editor_account, inbox_folder, drafts_folder, "send-draft",
        )
        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type(" Edited before sending.")
        expect(body).to_contain_text("Edited before sending.")

        page.get_by_role("button", name="Send", exact=True).click()

        # The prompt came up within half a second of pressing Send, and
        # stayed until the draft itself went away several seconds later --
        # checked first, since the editor's later disappearance takes the
        # prompt with it.
        with pytest.raises(AssertionError):
            expect(page.get_by_role("dialog", name="Save this message?")).to_be_visible(
                timeout=3_000,
            )
        # The editor is done the moment the send is accepted.
        expect(page.get_by_text("Editing draft")).not_to_be_visible(timeout=2_000)

        page.wait_for_timeout(_PAST_THE_UNDO_WINDOW_MS)
        sends = _sends_for(api_client, editor_account["id"], draft["subject"])
        assert len(sends) == 1, f"expected exactly one send, got {sends}"

        # Sending a draft leaves no draft behind: PostIMAP removes the one
        # it names once the send has landed.
        _trigger_sync(api_client, editor_account["id"])
        wait_for(
            lambda: all(
                m["id"] != draft["id"]
                for m in _list_folder(api_client, editor_account["id"], drafts_folder["id"])
            ) or None,
            timeout_s=60.0, description="Sent draft removed from Drafts",
        )

    def test_a_second_send_press_after_the_first_never_sends_twice(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        drafts_folder: dict[str, Any],
    ) -> None:
        """The exact sequence that sent twice: Send, then Escape out of
        whatever came up, then Send again wherever one is still offered."""
        draft = _reopen_a_saved_reply_draft(
            page, app_server, api_client, dovecot_endpoint,
            editor_account, inbox_folder, drafts_folder, "send-twice",
        )
        page.get_by_test_id("mail-editor-body").click()
        page.keyboard.type(" One more line.")

        page.get_by_role("button", name="Send", exact=True).click()
        page.wait_for_timeout(1_000)
        page.keyboard.press("Escape")
        send_buttons = page.get_by_role("button", name="Send", exact=True)
        for index in range(send_buttons.count()):
            if send_buttons.nth(index).is_visible():
                send_buttons.nth(index).click()
                break

        page.wait_for_timeout(_PAST_THE_UNDO_WINDOW_MS)
        sends = _sends_for(api_client, editor_account["id"], draft["subject"])
        assert len(sends) == 1, f"expected exactly one send, got {sends}"


class TestSendingStateIsImmediate:
    def test_send_shows_sending_at_once_and_a_failure_restores_the_composer(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        editor_account: dict[str, Any],
    ) -> None:
        """Pressing Send leaves the composer at once, into a visible sending
        state -- there is nothing left to press twice -- and a failure puts
        the composer back exactly as it was, with the error said."""
        subject = f"UI sending state {uuid.uuid4()}"
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_test_id("mail-editor-body").fill("Kept through a failed send.")

        # Hold the send request so both the in-flight state and the failure
        # are observed deterministically rather than raced.
        held: list[Any] = []

        def _hold_send(route: Any) -> None:
            if route.request.method == "POST":
                held.append(route)
            else:
                route.continue_()

        page.route(lambda url: url.split("?")[0].endswith("/api/outbox"), _hold_send)

        shown_within_a_frame = dialog.get_by_role("button", name="Send", exact=True).evaluate(
            """async (button) => {
                button.click();
                await new Promise((resolve) => requestAnimationFrame(resolve));
                return document.querySelector('[data-testid="compose-sending"]') !== null;
            }"""
        )
        assert shown_within_a_frame, "no sending state within one frame of pressing Send"

        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(100)
        assert held, "the send request never left the browser"
        expect(page.get_by_test_id("compose-sending")).to_be_visible()
        expect(dialog.get_by_role("button", name="Send", exact=True)).not_to_be_visible()

        held[0].fulfill(status=500, json={"detail": "Simulated failure"})

        expect(page.get_by_test_id("compose-sending")).not_to_be_visible(timeout=10_000)
        expect(dialog.get_by_role("alert")).to_contain_text("Simulated failure")
        expect(dialog.get_by_test_id("mail-editor-body")).to_contain_text(
            "Kept through a failed send.",
        )
        expect(dialog.get_by_role("textbox", name="Subject")).to_have_value(subject)
        assert _sends_for(api_client, editor_account["id"], subject) == []

        # The failure unlocked the composer rather than leaving it done --
        # the same Send now goes through, once.
        page.unroute_all()
        dialog.get_by_role("button", name="Send", exact=True).click()
        expect(dialog).not_to_be_visible(timeout=10_000)
        # Counted once the undo window has passed, which is also what keeps
        # this send's undo banner from still showing in the next test on
        # the same account.
        page.wait_for_timeout(_PAST_THE_UNDO_WINDOW_MS)
        sends = _sends_for(api_client, editor_account["id"], subject)
        assert len(sends) == 1, f"expected exactly one send, got {sends}"


class TestNavigatingAwayFromADirtyDraftPrompts:
    """A reopened draft is the one other composer in this app -- the same
    dirty-navigation guard ReplyBox registers for an in-progress reply
    was missing here, so switching folders while editing a reopened
    draft used to discard it with no prompt at all."""

    def test_switching_folders_with_a_dirty_reopened_draft_prompts(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        drafts_folder: dict[str, Any],
    ) -> None:
        _reopen_a_saved_reply_draft(
            page, app_server, api_client, dovecot_endpoint,
            editor_account, inbox_folder, drafts_folder, "draft-guard",
        )

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type(" More, not yet saved.")
        expect(body).to_contain_text("More, not yet saved.")

        # Switching folders is the same "leaves the current view" action
        # a row click or a keyboard shortcut is -- all of it used to
        # silently unmount this editor with the new text still in it.
        _open_folder(page, inbox_folder)
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Cancel", exact=True).click()
        expect(confirm).not_to_be_visible()
        expect(page.get_by_text("Editing draft")).to_be_visible()
        expect(body).to_contain_text("More, not yet saved.")

        _open_folder(page, inbox_folder)
        expect(confirm).to_be_visible(timeout=10_000)
        confirm.get_by_role("button", name="Discard", exact=True).click()
        expect(confirm).not_to_be_visible()
        expect(page.get_by_text("Editing draft")).to_have_count(0)


@pytest.fixture(scope="module")
def identity_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    """A second account, separate from editor_account, so giving it more
    than one identity cannot change what any other test in this module
    sees rendered."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("identity")
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


@pytest.fixture(scope="module")
def default_identity(
    api_client: httpx.Client, identity_account: dict[str, Any],
) -> dict[str, Any]:
    """An account's first identity is always made the default, whatever
    the request itself asks for."""
    resp = api_client.post(
        "/api/identities",
        json={"account_id": identity_account["id"], "address": identity_account["email"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def alias_identity(
    api_client: httpx.Client, identity_account: dict[str, Any], default_identity: dict[str, Any],
) -> dict[str, Any]:
    resp = api_client.post(
        "/api/identities",
        json={"account_id": identity_account["id"], "address": unique_email("identity-alias")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestIdentitySelection:
    """Which identity a reply or a fresh compose starts from -- already
    correct; these lock the fallback chain with a test that would fail if
    it ever regressed."""

    def test_a_reply_sends_from_the_address_the_original_arrived_at(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        mailpit_http_url: str,
        identity_account: dict[str, Any],
        default_identity: dict[str, Any],
        alias_identity: dict[str, Any],
    ) -> None:
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"Identity fallback test {uuid.uuid4()}"
        # The To: header names the alias -- the address matchIdentity
        # resolves against -- while the LMTP envelope recipient is the
        # account's own mailbox, since aliasing is an application-level
        # notion here rather than something the mail server routes.
        message = build_eml(
            sender="sender@example.com", recipient=alias_identity["address"], subject=subject,
            message_id=f"<identity-{uuid.uuid4()}@example.com>",
            body="Addressed to the alias.",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="sender@example.com", recipient=identity_account["email"],
        )

        inbox = wait_for_folder(api_client, str(identity_account["id"]), "INBOX")
        original = wait_for(
            lambda: next(
                (m for m in _list_folder(api_client, identity_account["id"], inbox["id"])
                 if m["subject"] == subject),
                None,
            ),
            description=f"{subject!r} synced into INBOX",
        )

        page.goto(app_server)
        select_account(page, identity_account)
        mail_row(page, original["id"]).click()
        page.get_by_role("button", name="Reply", exact=True).click()

        # Scoped to the "From" identity row specifically, not just any
        # select-trigger on the page -- the compose dialog's own "From
        # account" select (shown once more than one account exists) can
        # otherwise resolve alongside it when an account happens to share
        # its identity's own address as its name, the way these fixtures do.
        from_row = page.get_by_text("From", exact=True).locator("xpath=..")
        from_trigger = from_row.locator('[data-slot="select-trigger"]')
        expect(from_trigger.get_by_text(alias_identity["address"], exact=True)).to_be_visible(
            timeout=10_000,
        )

        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible(timeout=10_000)

        reply_subject = f"Re: {subject}"
        mailpit_message = wait_for_mailpit_message(mailpit_http_url, reply_subject)
        raw = httpx.get(
            f"{mailpit_http_url}/api/v1/message/{mailpit_message['ID']}/raw", timeout=10.0,
        )
        assert raw.status_code == 200, raw.text
        from_header = re.search(r"^From:.*$", raw.text, re.MULTILINE)
        assert from_header and alias_identity["address"] in from_header.group(), (
            f"expected the reply's From header to carry {alias_identity['address']!r}; "
            f"raw source:\n{raw.text}"
        )

    def test_a_fresh_compose_uses_the_accounts_starred_default_identity(
        self,
        page: Page,
        app_server: str,
        identity_account: dict[str, Any],
        default_identity: dict[str, Any],
        alias_identity: dict[str, Any],
    ) -> None:
        page.goto(app_server)
        select_account(page, identity_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")

        # Scoped to the "From" identity row -- see the identical comment on
        # test_a_reply_sends_from_the_address_the_original_arrived_at above.
        from_row = dialog.get_by_text("From", exact=True).locator("xpath=..")
        from_trigger = from_row.locator('[data-slot="select-trigger"]')
        expect(
            from_trigger.get_by_text(default_identity["address"], exact=True)
        ).to_be_visible(timeout=10_000)


class TestCloseAndDiscard:
    def test_closing_a_dirty_reply_prompts_to_save_or_discard(
        self,
        page: Page,
        app_server: str,
        editor_account: dict[str, Any],
        original_message: dict[str, Any],
    ) -> None:
        """The gap that most annoyed the owner: no way out of a reply in
        progress at all. Typing, then Close, has to ask rather than
        silently drop what was typed."""
        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A reply in progress.")

        page.get_by_role("button", name="Close", exact=True).click()
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Discard", exact=True).click()
        expect(confirm).not_to_be_visible()
        # Back to the collapsed Reply/Reply all/Forward row -- the typed
        # text is gone, discarded rather than left stranded on screen.
        expect(page.get_by_role("button", name="Reply", exact=True)).to_be_visible()

    def test_closing_a_clean_reply_needs_no_prompt(
        self,
        page: Page,
        app_server: str,
        editor_account: dict[str, Any],
        original_message: dict[str, Any],
    ) -> None:
        _open_thread(page, app_server, editor_account, original_message)
        page.get_by_role("button", name="Reply", exact=True).click()
        expect(page.get_by_role("button", name="Close", exact=True)).to_be_visible()

        page.get_by_role("button", name="Close", exact=True).click()
        expect(page.get_by_role("dialog", name="Save this message?")).not_to_be_visible()
        expect(page.get_by_role("button", name="Reply", exact=True)).to_be_visible()

    def test_escaping_a_dirty_compose_dialog_prompts_instead_of_discarding_silently(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        dialog.get_by_test_id("mail-editor-body").click()
        dialog.get_by_test_id("mail-editor-body").type("Unsaved compose text.")

        page.keyboard.press("Escape")
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Cancel", exact=True).click()
        expect(confirm).not_to_be_visible()
        # The compose dialog's own content survived underneath -- Escape
        # did not silently discard it, the actual failure being guarded.
        expect(page.get_by_test_id("mail-editor-body")).to_contain_text(
            "Unsaved compose text.",
        )


class TestDoubleSubmitGuard:
    def test_double_clicking_send_queues_the_message_only_once(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        editor_account: dict[str, Any],
    ) -> None:
        """Nothing guarded the mutation itself, only the button's disabled
        attribute -- react-query's own isPending is the state as of the
        last render, so two clicks landing before React re-renders both
        read it as false and both fire."""
        subject = f"UI double-send test {uuid.uuid4()}"
        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("button", name="Compose", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Message")
        dialog.get_by_role("combobox", name="To").fill("recipient@example.com")
        dialog.get_by_role("textbox", name="Subject").fill(subject)
        dialog.get_by_test_id("mail-editor-body").fill("Sent from a double-click.")

        dialog.get_by_role("button", name="Send", exact=True).dblclick()
        # A staged send reports itself through the undo banner rather than
        # a toast -- the two would say the same thing twice, and the banner
        # is where cancelling lives. Waiting on the banner is what says the
        # send was accepted; the count below is what this test is about.
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible(timeout=10_000)

        def _outbox_rows() -> list[dict[str, Any]] | None:
            resp = api_client.get("/api/outbox", params={"account_id": editor_account["id"]})
            assert resp.status_code == 200, resp.text
            rows = [row for row in resp.json() if row["subject"] == subject]
            return rows or None

        wait_for(_outbox_rows, description=f"Outbox row for {subject!r}")
        # A genuine second send would already have landed by the time the
        # first one settles -- give it the same window rather than checking
        # the instant the first row appears.
        page.wait_for_timeout(1500)
        rows = _outbox_rows() or []
        assert len(rows) == 1, f"expected exactly one outbox row for {subject!r}, got {rows}"


class TestTrashingTheOpenMessageKeepsAnInProgressReply:
    def test_trashing_the_message_you_are_replying_to_keeps_the_reply_open(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Reported as: reply box open with typed text, hover the row,
        Move to trash -- the message and the reply box both vanished with
        no prompt, and Undo only restored the message, not the reply.
        Trashing (unlike sending) is already reversible on its own, so the
        fix is to stop the reading pane from unmounting the reply, not to
        prompt before an action that was never in question."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"UI trash-keeps-reply test {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=editor_account["email"], subject=subject,
            message_id=f"<editor-trash-{uuid.uuid4()}@example.com>",
            body="Original body for the trash-keeps-reply test.",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="sender@example.com", recipient=editor_account["email"],
        )

        def _find() -> dict[str, Any] | None:
            resp = api_client.get(
                f"/api/accounts/{editor_account['id']}/messages",
                params={"folder_id": inbox_folder["id"]},
            )
            return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)

        target = wait_for(_find, description=f"{subject!r} synced into INBOX")

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, target["id"]).click()
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A reply worth keeping.")
        expect(body).to_contain_text("A reply worth keeping.")

        row = mail_row(page, target["id"])
        row.hover()
        row.get_by_title("Move to trash").click()
        expect(page.get_by_text("Moved to trash")).to_be_visible(timeout=10_000)

        # The reply box must not have been unmounted along with the reading
        # pane's old content -- the typed text is still exactly what it
        # was, not silently discarded.
        expect(page.get_by_test_id("mail-editor-body")).to_contain_text(
            "A reply worth keeping.",
        )


class TestTrashingAnOlderThreadMessageKeepsAnInProgressReply:
    # Two message deliveries plus a poll confirming they actually joined one
    # thread before driving the browser at all -- legitimately slower than
    # this module's other tests, which deliver one message each.
    @pytest.mark.timeout(240)
    def test_trashing_an_older_message_in_the_thread_keeps_the_reply_open(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """A reply always targets the thread's newest message, while the
        reading pane's own "open" message (mailId) can be an older one the
        reader expanded within the same thread -- in the flat (non-threaded)
        list each message is its own row, so trashing the older one
        specifically is reachable on its own, and must not discard a reply
        against the newest either. This is what matching on the thread
        rather than on the single message id closes."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        stem = uuid.uuid4()
        older_subject = f"UI thread-keeps-reply older {stem}"
        newer_subject = f"Re: {older_subject}"
        older_message_id = f"<older-{stem}@example.com>"

        older = build_eml(
            sender="sender@example.com", recipient=editor_account["email"],
            subject=older_subject, message_id=older_message_id,
            body="Older message in the thread.",
        )
        deliver_message(
            older, host, lmtp_port,
            sender="sender@example.com", recipient=editor_account["email"],
        )

        def _find_older() -> dict[str, Any] | None:
            resp = api_client.get(
                f"/api/accounts/{editor_account['id']}/messages",
                params={"folder_id": inbox_folder["id"]},
            )
            return next(
                (m for m in resp.json()["messages"] if m["subject"] == older_subject), None,
            )

        older_target = wait_for(_find_older, description=f"{older_subject!r} synced into INBOX")

        newer = build_eml(
            sender="sender@example.com", recipient=editor_account["email"],
            subject=newer_subject, message_id=f"<newer-{stem}@example.com>",
            in_reply_to=older_message_id,
            body="Newer message in the thread.",
        )
        deliver_message(
            newer, host, lmtp_port,
            sender="sender@example.com", recipient=editor_account["email"],
        )

        def _find_newer_on_same_thread() -> dict[str, Any] | None:
            resp = api_client.get(
                f"/api/accounts/{editor_account['id']}/messages",
                params={"folder_id": inbox_folder["id"]},
            )
            match = next(
                (m for m in resp.json()["messages"] if m["subject"] == newer_subject), None,
            )
            # Confirmed joined onto the older message's own thread --
            # otherwise the rest of this test would prove nothing at all.
            return match if match and match["thread_id"] == older_target["thread_id"] else None

        wait_for(
            _find_newer_on_same_thread,
            description=f"{newer_subject!r} synced onto the same thread as the older message",
        )

        page.goto(app_server)
        select_account(page, editor_account)
        page.get_by_role("switch", name="Group by conversation").click()

        mail_row(page, older_target["id"]).click()
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A reply against the newest message.")
        expect(body).to_contain_text("A reply against the newest message.")

        row = mail_row(page, older_target["id"])
        row.hover()
        row.get_by_title("Move to trash").click()
        expect(page.get_by_text("Moved to trash")).to_be_visible(timeout=10_000)

        expect(page.get_by_test_id("mail-editor-body")).to_contain_text(
            "A reply against the newest message.",
        )

        # Left as found, for whatever else in this module runs after it.
        page.get_by_role("switch", name="Group by conversation").click()


class TestBulkActionKeepsAnInProgressReply:
    def test_a_bulk_action_covering_the_open_message_returns_to_it_afterward(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """Checking a second row replaces the reading pane with the bulk
        panel regardless -- that part is not in question. What the bulk
        path got wrong is what happens once the panel goes away again: the
        single-message action path already keeps the selection on a
        message whose reply is dirty rather than clearing it, and the bulk
        path carried a comment claiming the same reasoning while never
        actually checking anything -- so the reading pane came back empty
        instead of reopening the very message the reply belongs to."""
        host, _imap_port, lmtp_port = dovecot_endpoint
        stem = uuid.uuid4()
        target_subject = f"UI bulk-keeps-reply target {stem}"
        other_subject = f"UI bulk-keeps-reply other {stem}"
        for subject in (target_subject, other_subject):
            message = build_eml(
                sender="sender@example.com", recipient=editor_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
                body="Body for the bulk-keeps-reply test.",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=editor_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, editor_account["id"], inbox_folder["id"])
                if m["subject"] in (target_subject, other_subject)
            ]
            return found if len(found) == 2 else None

        targets = wait_for(
            _find_all, description="both bulk-keeps-reply messages synced into INBOX",
        )
        target = next(m for m in targets if m["subject"] == target_subject)

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, target["id"]).click()
        expect(page.get_by_role("heading", name=target_subject, exact=True)).to_be_visible(
            timeout=15_000,
        )
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A reply worth keeping.")
        expect(body).to_contain_text("A reply worth keeping.")

        rows = [mail_row(page, m["id"]) for m in targets]
        for row in rows:
            expect(row).to_be_visible(timeout=15_000)
        rows[0].hover()
        for row in rows:
            row.get_by_role("checkbox").click()

        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Move to trash", exact=True,
        ).click()
        for row in rows:
            expect(row).not_to_be_visible(timeout=10_000)

        # The bulk panel replacing the reading pane while both rows were
        # checked is expected; the selection clearing once the action
        # settles brings the reading pane back, and it must point at the
        # same message rather than showing nothing.
        expect(page.get_by_role("heading", name=target_subject, exact=True)).to_be_visible(
            timeout=15_000,
        )


class TestNavigatingAwayFromADirtyReplyPrompts:
    """Clicking a different message while a reply holds unsaved text used
    to close the reply and take the text with it silently, in the split
    view -- the same three-way choice the Close button already offers,
    now covering any selection change rather than only an explicit close."""

    @staticmethod
    def _deliver_and_open_first(
        page: Page, app_server: str, api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int], editor_account: dict[str, Any],
        inbox_folder: dict[str, Any], stem: object,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host, _imap_port, lmtp_port = dovecot_endpoint
        first_subject = f"UI nav-prompt first {stem}"
        second_subject = f"UI nav-prompt second {stem}"
        for subject in (first_subject, second_subject):
            message = build_eml(
                sender="sender@example.com", recipient=editor_account["email"], subject=subject,
                message_id=f"<{uuid.uuid4()}@example.com>",
                body="Body for the navigation-prompt test.",
            )
            deliver_message(
                message, host, lmtp_port,
                sender="sender@example.com", recipient=editor_account["email"],
            )

        def _find_all() -> list[dict[str, Any]] | None:
            found = [
                m for m in _list_folder(api_client, editor_account["id"], inbox_folder["id"])
                if m["subject"] in (first_subject, second_subject)
            ]
            return found if len(found) == 2 else None

        found = wait_for(_find_all, description="both nav-prompt messages synced into INBOX")
        first = next(m for m in found if m["subject"] == first_subject)
        second = next(m for m in found if m["subject"] == second_subject)

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, first["id"]).click()
        expect(page.get_by_role("heading", name=first_subject, exact=True)).to_be_visible(
            timeout=15_000,
        )
        page.get_by_role("button", name="Reply", exact=True).click()

        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A reply not ready to lose.")
        expect(body).to_contain_text("A reply not ready to lose.")

        return first, second

    def test_cancelling_the_prompt_keeps_the_reply_open(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        first, second = self._deliver_and_open_first(
            page, app_server, api_client, dovecot_endpoint, editor_account, inbox_folder,
            uuid.uuid4(),
        )

        mail_row(page, second["id"]).click()
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Cancel", exact=True).click()
        expect(confirm).not_to_be_visible()
        # Still on the first message with the reply intact -- the click on
        # the second row did not silently navigate away underneath the
        # dialog.
        expect(page.get_by_role("heading", name=first["subject"], exact=True)).to_be_visible()
        expect(page.get_by_test_id("mail-editor-body")).to_contain_text(
            "A reply not ready to lose.",
        )

    def test_discarding_the_prompt_opens_the_other_message(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        _first, second = self._deliver_and_open_first(
            page, app_server, api_client, dovecot_endpoint, editor_account, inbox_folder,
            uuid.uuid4(),
        )

        mail_row(page, second["id"]).click()
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Discard", exact=True).click()
        expect(confirm).not_to_be_visible()
        expect(page.get_by_role("heading", name=second["subject"], exact=True)).to_be_visible(
            timeout=10_000,
        )
        # Collapsed reply/reply-all/forward row -- the discarded reply is
        # gone, not stranded behind the newly opened message.
        expect(page.get_by_role("button", name="Reply", exact=True)).to_be_visible()

    def test_saving_a_draft_from_the_prompt_opens_the_other_message(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        _first, second = self._deliver_and_open_first(
            page, app_server, api_client, dovecot_endpoint, editor_account, inbox_folder,
            uuid.uuid4(),
        )

        mail_row(page, second["id"]).click()
        confirm = page.get_by_role("dialog", name="Save this message?")
        expect(confirm).to_be_visible(timeout=10_000)

        confirm.get_by_role("button", name="Save draft", exact=True).click()
        expect(confirm).not_to_be_visible()
        expect(page.get_by_text("Draft saved")).to_be_visible(timeout=10_000)
        expect(page.get_by_role("heading", name=second["subject"], exact=True)).to_be_visible(
            timeout=10_000,
        )


class TestLeavingADirtyReplyUnresolvedDoesNotHauntTheNextOne:
    """Navigating fully away from Mail (Calendar, Contacts, ...) while a
    reply holds unsaved text unmounts it with no prompt -- that gap is
    accepted. What isn't: the account switcher, reachable from every page,
    also asks to hold up a mail selection whenever a reply is dirty, and it
    still thought this one was, long after it was gone. Left unresolved,
    the next reply anywhere in the app used to open the save-or-discard
    prompt the instant it became dirty, with nothing on screen to explain
    why, and Discard on that prompt closed the reading pane and took the
    new reply's own text with it."""

    def test_a_stale_pending_selection_does_not_surface_on_the_next_reply(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        editor_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        host, _imap_port, lmtp_port = dovecot_endpoint
        subject = f"UI stale-block {uuid.uuid4()}"
        message = build_eml(
            sender="sender@example.com", recipient=editor_account["email"], subject=subject,
            message_id=f"<{uuid.uuid4()}@example.com>",
            body="Body for the stale-block test.",
        )
        deliver_message(
            message, host, lmtp_port,
            sender="sender@example.com", recipient=editor_account["email"],
        )

        def _find() -> dict[str, Any] | None:
            found = [
                m for m in _list_folder(api_client, editor_account["id"], inbox_folder["id"])
                if m["subject"] == subject
            ]
            return found[0] if found else None

        target = wait_for(_find, description="the stale-block message synced into INBOX")

        page.goto(app_server)
        select_account(page, editor_account)
        mail_row(page, target["id"]).click()
        expect(page.get_by_role("heading", name=subject, exact=True)).to_be_visible(
            timeout=15_000,
        )
        page.get_by_role("button", name="Reply", exact=True).click()
        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A first reply, abandoned by leaving the page.")
        expect(body).to_contain_text("A first reply, abandoned by leaving the page.")

        # Leave Mail entirely -- unmounts the reply with no prompt, by
        # design. The dirty marker it leaves behind is not cleared by this
        # navigation.
        page.get_by_role("link", name="Calendar", exact=True).click()
        page.wait_for_timeout(800)

        # The account switcher is reachable from every page and asks to
        # hold up a mail selection -- exactly the call site that used to
        # get stuck blocked on the now-gone reply's stale dirty marker.
        select_account(page, editor_account)

        page.get_by_role("link", name="Mail", exact=True).click()
        expect(page.get_by_role("heading", name=subject, exact=True)).to_be_visible(
            timeout=15_000,
        )

        # A fresh reply on this fresh view of the message must not surface
        # an unprompted "Save this message?" the moment it becomes dirty.
        page.get_by_role("button", name="Reply", exact=True).click()
        body = page.get_by_test_id("mail-editor-body")
        body.click()
        body.type("A second reply that must not be interrupted.")
        expect(body).to_contain_text("A second reply that must not be interrupted.")

        with pytest.raises(AssertionError):
            expect(page.get_by_role("dialog", name="Save this message?")).to_be_visible(
                timeout=3_000,
            )
        expect(body).to_contain_text("A second reply that must not be interrupted.")
