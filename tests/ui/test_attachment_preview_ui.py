"""
The attachment preview overlay: opening an image or a PDF full-screen from
its chip on a received message, rendering it in the browser rather than
downloading it, with the download control still offered beside it. A type
the overlay does not render falls back to a "no preview" message with
Download instead of ever fetching its bytes.
"""

from __future__ import annotations

import base64
import email.policy
import uuid
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.setup.mail_delivery import deliver_message
from tests.ui.helpers import (
    mail_row,
    select_account,
    unique_email,
    wait_for,
    wait_for_account_active,
    wait_for_folder,
)

from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD  # isort: skip

# A real 1x1 GIF, not a placeholder string -- the browser needs actual
# image bytes to decode and paint.
_TINY_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")


def _build_minimal_pdf() -> bytes:
    """A single blank page with a byte-accurate xref table, computed as the
    objects are written rather than hand-counted -- pdf.js's own recovery
    from a broken xref is not something to lean on for a test fixture."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << >> >>",
    ]
    header = b"%PDF-1.4\n"
    body = bytearray()
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(header) + len(body))
        body += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(header) + len(body)
    xref = f"xref\n0 {len(objects) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return header + bytes(body) + xref + trailer


def _build_message_with_attachments(*, sender: str, recipient: str, subject: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4()}@example.com>"
    msg.set_content("See attached.")
    msg.add_attachment(_TINY_GIF, maintype="image", subtype="gif", filename="photo.gif")
    msg.add_attachment(
        _build_minimal_pdf(), maintype="application", subtype="pdf", filename="report.pdf",
    )
    msg.add_attachment(
        b"not a real archive", maintype="application", subtype="zip", filename="archive.zip",
    )
    return msg.as_bytes(policy=email.policy.SMTP)


@pytest.fixture(scope="module")
def attachments_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    email_addr = unique_email("attach")
    resp = api_client.post(
        "/api/accounts",
        json={
            "name": email_addr,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email_addr,
            "imap_password": DOVECOT_PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(api_client, account["id"])
    account["email"] = email_addr
    return account


@pytest.fixture(scope="module")
def inbox_folder(api_client: httpx.Client, attachments_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(attachments_account["id"]), "INBOX")


@pytest.fixture(scope="module")
def message_with_attachments(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    attachments_account: dict[str, Any],
    inbox_folder: dict[str, Any],
) -> dict[str, Any]:
    host, _imap_port, lmtp_port = dovecot_endpoint
    subject = "UI attachment preview test"
    recipient = attachments_account["email"]
    message = _build_message_with_attachments(
        sender="sender@example.com", recipient=recipient, subject=subject,
    )
    deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=recipient)

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{attachments_account['id']}/messages",
            params={"folder_id": inbox_folder["id"]},
        )
        assert resp.status_code == 200, resp.text
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_find, description=f"{subject!r} synced into INBOX")


def _open_message(
    page: Page, app_server: str, account: dict[str, Any], message: dict[str, Any],
) -> None:
    page.goto(app_server)
    select_account(page, account)
    mail_row(page, message["id"]).click()
    expect(page.locator('[data-testid="email-body"]')).to_be_visible(timeout=15_000)


class TestAttachmentPreview:
    def test_image_attachment_previews_full_screen_with_download_still_offered(
        self,
        page: Page,
        app_server: str,
        attachments_account: dict[str, Any],
        message_with_attachments: dict[str, Any],
    ) -> None:
        _open_message(page, app_server, attachments_account, message_with_attachments)

        page.get_by_label("Preview photo.gif").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=10_000)
        expect(dialog.locator("img")).to_be_visible(timeout=10_000)
        expect(dialog.get_by_label("Download photo.gif")).to_be_visible()

        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible(timeout=10_000)

    def test_pdf_attachment_renders_a_page_onto_canvas(
        self,
        page: Page,
        app_server: str,
        attachments_account: dict[str, Any],
        message_with_attachments: dict[str, Any],
    ) -> None:
        _open_message(page, app_server, attachments_account, message_with_attachments)

        page.get_by_label("Preview report.pdf").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=10_000)

        canvas = dialog.locator("canvas")
        expect(canvas).to_be_visible(timeout=15_000)
        box = canvas.bounding_box()
        assert box is not None
        assert box["width"] > 0 and box["height"] > 0, (
            f"canvas has no rendered size: {box!r}"
        )

    def test_unsupported_attachment_shows_no_preview_and_never_fetches_it(
        self,
        page: Page,
        app_server: str,
        attachments_account: dict[str, Any],
        message_with_attachments: dict[str, Any],
    ) -> None:
        _open_message(page, app_server, attachments_account, message_with_attachments)

        requests: list[str] = []
        page.on("request", lambda req: requests.append(req.url))

        page.get_by_label("Preview archive.zip").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("No preview available for this file.")).to_be_visible(
            timeout=10_000,
        )
        expect(dialog.get_by_label("Download archive.zip")).to_be_visible()
        expect(dialog.locator("img")).to_have_count(0)
        expect(dialog.locator("canvas")).to_have_count(0)
        assert not any("attachments" in url for url in requests), (
            "an ineligible attachment's bytes were fetched even though nothing "
            "renders them"
        )
