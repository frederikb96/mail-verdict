"""
The message renderer: what a real message's own markup can and cannot do
to the reading pane, and the controls layered over it that do not depend
on any one message's content -- the per-message dark-mode toggle and the
sender avatar. Every test here delivers real HTML over LMTP and asserts
against the rendered reading pane, never against the sanitizer's output
string directly -- tests/unit/test_sanitizer.py already covers that layer
in isolation.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    mail_row,
    select_account,
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

# A real GIF, not a placeholder string -- a browser needs actual image bytes
# to compute box dimensions from, and an oversized width/height attribute is
# the sender-declared half of "no dimensions must not mean enormous": this
# proves the declared half is contained too, not only the undeclared one.
_TINY_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


@pytest.fixture(scope="module")
def rendering_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    email = unique_email("render")
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
def inbox_folder(api_client: httpx.Client, rendering_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(rendering_account["id"]), "INBOX")


def _list_folder(api_client: httpx.Client, account_id: str, folder_id: str) -> list[dict[str, Any]]:
    resp = api_client.get(f"/api/accounts/{account_id}/messages", params={"folder_id": folder_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _deliver_html(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account_id: str,
    recipient: str,
    inbox_folder_id: str,
    subject: str,
    html: str,
    sender: str = "sender@example.com",
) -> dict[str, Any]:
    """Deliver a single-part HTML message and wait for it to sync into INBOX.

    `sender` is the `From:` header, which may carry a display name -- the
    LMTP envelope's MAIL FROM cannot, so that goes over the bare address
    parsed out of it.
    """
    host, _imap_port, lmtp_port = dovecot_endpoint
    envelope_sender = sender.split("<")[-1].rstrip(">") if "<" in sender else sender
    message = build_eml(
        sender=sender, recipient=recipient, subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>",
        body=html, content_type="text/html; charset=utf-8",
    )
    deliver_message(message, host, lmtp_port, sender=envelope_sender, recipient=recipient)

    def _find() -> dict[str, Any] | None:
        for m in _list_folder(api_client, account_id, inbox_folder_id):
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_find, description=f"{subject!r} synced into INBOX")


class TestOversizedAndHostileMarkupIsContained:
    """A message cannot lay itself out over the application, and an image
    with no sizing of its own -- or a hostile one declaring an absurd
    size -- cannot dominate the reading pane either."""

    def test_an_oversized_declared_image_stays_within_the_pane(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI oversized image test",
            f'<p>Before</p><img src="{_TINY_GIF}" width="5000" height="5000"><p>After</p>',
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        body = page.locator('[data-testid="email-body"]')
        img = body.locator("img")
        expect(img).to_be_visible(timeout=15_000)

        body_box = body.bounding_box()
        img_box = img.bounding_box()
        assert body_box is not None and img_box is not None
        assert img_box["height"] <= body_box["height"] + 1, (
            f"image height {img_box['height']} exceeds the reading pane's "
            f"{body_box['height']}"
        )
        assert img_box["width"] <= body_box["width"] + 1

    def test_hostile_positioning_around_an_oversized_image_does_not_escape(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        """An oversized image wrapped in the fixed-full-viewport-overlay
        shape a hostile sender would use to cover the whole application."""
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI hostile overlay test",
            '<a href="https://attacker.example/phish">'
            '<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:999999">'
            f'<img src="{_TINY_GIF}" width="5000" height="5000">'
            "</div></a>",
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        body = page.locator('[data-testid="email-body"]')
        expect(body.locator("img")).to_be_visible(timeout=15_000)

        # A sidebar element is well outside the reading pane -- if the
        # overlay had actually escaped to cover the viewport, this would
        # not be the sidebar's own control underneath the click.
        sidebar_trigger = page.locator('[data-slot="sidebar-header"]').get_by_role("button").first
        if sidebar_trigger.is_visible():
            box = sidebar_trigger.bounding_box()
            assert box is not None
            hit = page.evaluate(
                "([x, y]) => { const el = document.elementFromPoint(x, y); "
                "return el ? el.closest('[data-slot=\"sidebar-header\"]') !== null : false; }",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
            )
            assert hit, "the message's fixed overlay covers the sidebar"


class TestStructuralMarkupDoesNotLeakAsVisibleCopy:
    def test_a_head_title_does_not_render_as_body_text(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI title leak test",
            "<html><head><title>Leaked newsletter title</title>"
            "<style>body{color:red}</style></head>"
            "<body><p>Real visible content</p></body></html>",
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        body = page.locator('[data-testid="email-body"]')
        expect(body.get_by_text("Real visible content")).to_be_visible(timeout=15_000)
        with pytest.raises(AssertionError):
            expect(body.get_by_text("Leaked newsletter title")).to_be_visible(timeout=1_000)


class TestPerMessageDarkMode:
    """Every message renders on a light canvas by default -- correct, since
    mail is written assuming one -- but the reader can ask for dark, and a
    message that declares its own dark-canvas support gets it without being
    asked."""

    def test_the_toggle_switches_the_canvas_and_the_choice_survives_reopening(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI dark toggle test",
            "<p>Ordinary light-assuming mail</p>",
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        body = page.locator('[data-testid="email-body"]')
        expect(body).to_be_visible(timeout=15_000)
        toggle = page.get_by_role("button", name="Enable dark message mode", exact=True)
        expect(toggle).to_be_visible(timeout=10_000)

        def _host_background() -> str:
            return body.evaluate(
                "(el) => getComputedStyle(el.shadowRoot.host).backgroundColor"
            )

        light_mode_button = page.get_by_role(
            "button", name="Switch this message to light mode", exact=True,
        )

        light_bg = _host_background()
        toggle.click()
        expect(light_mode_button).to_be_visible(timeout=10_000)
        dark_bg = _host_background()
        assert dark_bg != light_bg

        # Collapse and re-expand the same message -- the choice must not
        # reset just because the component unmounted its shadow content.
        header = page.locator('[data-testid="thread-message-header"]')
        header.click()
        header.click()
        expect(light_mode_button).to_be_visible(timeout=10_000)
        assert _host_background() == dark_bg

    def test_a_message_declaring_dark_support_renders_dark_unasked(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        browser: Browser,
    ) -> None:
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI dark declared test",
            '<meta name="color-scheme" content="light dark">'
            "<p>Renders dark on its own</p>",
        )

        # A dedicated dark-scheme context: the shared page fixture's colour
        # scheme is whatever the host session left it at, and this is the
        # one assertion in the module that depends on it being dark.
        context = browser.new_context(color_scheme="dark")
        dark_page = context.new_page()
        try:
            dark_page.goto(app_server)
            select_account(dark_page, rendering_account)
            mail_row(dark_page, target["id"]).click()

            body = dark_page.locator('[data-testid="email-body"]')
            expect(body).to_be_visible(timeout=15_000)
            light_mode_button = dark_page.get_by_role(
                "button", name="Switch this message to light mode", exact=True,
            )
            expect(light_mode_button).to_be_visible(timeout=10_000)
        finally:
            context.close()


class TestSenderAvatar:
    """Initials render for every sender with no network involved; a remote
    avatar is only ever attempted for a sender already on the image
    allowlist, and never for one who is not."""

    def test_a_non_allowlisted_sender_gets_initials_and_no_network_request(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
    ) -> None:
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI avatar privacy test",
            "<p>hello</p>",
            sender="Avatar Sender <avatar-sender@example.com>",
        )

        remote_requests: list[str] = []
        page.on(
            "request",
            lambda req: remote_requests.append(req.url)
            if "gravatar" in req.url
            else None,
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        expect(page.locator('[data-testid="email-body"]')).to_be_visible(timeout=15_000)
        header = page.locator('[data-testid="thread-message-header"]')
        fallback = header.locator('[data-slot="avatar-fallback"]')
        expect(fallback).to_have_text("AS", timeout=10_000)
        assert remote_requests == [], f"unexpected remote avatar fetch: {remote_requests}"
