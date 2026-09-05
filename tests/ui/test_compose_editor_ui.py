"""
The rich-text composer: pasted HTML keeps its formatting, long content
scrolls inside its own box rather than growing it without bound, a reply
embeds the original as a real quote rather than a lossy text dump, and
every composer surface can be closed -- with a prompt to save or discard
when there is unsaved work, the gap that most annoyed the owner.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
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
    folder(page, folder_row["id"]).get_by_role("button").click()


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

    def test_a_pasted_table_keeps_a_separator_between_cells(
        self, page: Page, app_server: str, editor_account: dict[str, Any],
    ) -> None:
        """The editor's schema has no table node, so a pasted one is
        flattened -- correctly dropping the table but not the gap between
        cells. Without that gap the two cells' text runs together
        ("cell Acell B"), reading as corrupted rather than simplified."""
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

        expect(body).to_contain_text("cell A")
        expect(body).to_contain_text("cell B")
        expect(body).not_to_contain_text("Acell")

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
        expect(page.get_by_text("Message queued for sending")).to_be_visible(timeout=10_000)

        mailpit_message = wait_for_mailpit_message(mailpit_http_url, subject)
        raw = httpx.get(
            f"{mailpit_http_url}/api/v1/message/{mailpit_message['ID']}/raw", timeout=10.0,
        )
        assert raw.status_code == 200, raw.text
        assert "> Original plain body line." in raw.text, (
            "expected the plain-text quote to survive the reopened draft; "
            f"raw source:\n{raw.text}"
        )


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
