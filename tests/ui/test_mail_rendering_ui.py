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

import asyncio
import base64
import concurrent.futures
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, Request, expect

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from tests.e2e.helpers import wait_for_dav_account_active, wait_for_dav_collection
from tests.setup.dav_helpers import create_addressbook, discover
from tests.setup.large_mailbox import build_large_mailbox
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
    RADICALE_ALIAS,
    RADICALE_PORT,
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


@pytest.fixture(scope="module")
def avatar_addressbook_owner(radicale_endpoint: tuple[str, int]) -> str:
    """An address book on the real Radicale server, owned by a fresh
    principal -- see test_contacts_ui.py's `ui_addressbook_owner`, the
    same pattern for the same reason."""
    host, port = radicale_endpoint
    base_url = f"http://{host}:{port}/"
    username = f"avatar-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, base_url)
        create_addressbook(client, principal, "avatar-book", "Avatar Book")
    return username


@pytest.fixture(scope="module")
def avatar_addressbook(
    api_client: httpx.Client, avatar_addressbook_owner: str,
) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": avatar_addressbook_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    dav_account = resp.json()
    wait_for_dav_account_active(api_client, dav_account["id"])
    return wait_for_dav_collection(api_client, dav_account["id"], "Avatar Book")


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
    """A message renders on a light canvas by default -- correct, since mail
    is written assuming one -- unless it declares its own dark-mode support,
    in which case it opens dark straight away; either way the reader can
    override with the per-message toggle."""

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

    def test_a_message_declaring_dark_support_opens_dark_and_its_own_rule_applies(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        browser: Browser,
    ) -> None:
        """The shape a real ESP uses: an inline colour for the common case,
        overridden with `!important` in an `@media (prefers-color-scheme:
        dark)` block for a reader whose environment is actually dark --
        `!important` is what real dark-mode email CSS guides all call for,
        since an inline style otherwise always outranks a class rule
        regardless of whether its media query matched. The stylesheet
        survives sanitisation now, so the reading pane reads the
        declaration and opens dark without a click -- and, since this
        test's own browser context is genuinely dark, the message's own
        media query fires for real too, so the light-mode inline colour is
        never what is shown."""
        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI dark declared test",
            "<style>@media (prefers-color-scheme: dark) "
            "{ .msg { color: #eeeeee !important } }</style>"
            '<p class="msg" style="color:#111111">Assumes its own dark rules survived</p>',
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
            # Already dark: the toggle offers to switch *back* to light,
            # rather than offering to enable dark -- the declaration alone
            # is enough, with no click needed.
            toggle = dark_page.get_by_role(
                "button", name="Switch this message to light mode", exact=True,
            )
            expect(toggle).to_be_visible(timeout=10_000)

            msg = body.locator(".msg")
            expect(msg).to_have_css("color", "rgb(238, 238, 238)")
        finally:
            context.close()


class TestSenderAvatar:
    """A sender with no matching address-book contact gets initials --
    derived from the display name, with no network request of any kind.
    A sender who does match one gets that contact's photo instead, when it
    has one: an embedded photo with no request of its own, a remote photo
    URL only once the sender is allowlisted the same way any other remote
    image is. Never a lookup keyed by address against an unrelated third
    party (Gravatar) -- that was considered and rejected, since it would
    tell that party a message from the address was opened, for every
    sender, whether or not the sender is allowlisted."""

    def test_a_sender_gets_initials_and_no_network_request_at_all(
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

        third_party_requests: list[str] = []

        def _record(req: object) -> None:
            url = req.url  # type: ignore[attr-defined]
            if not url.startswith(app_server):
                third_party_requests.append(url)

        page.on("request", _record)

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        expect(page.locator('[data-testid="email-body"]')).to_be_visible(timeout=15_000)
        header = page.locator('[data-testid="thread-message-header"]')
        fallback = header.locator('[data-slot="avatar-fallback"]')
        expect(fallback).to_have_text("AS", timeout=10_000)
        assert third_party_requests == [], f"unexpected third-party request: {third_party_requests}"

    def test_a_sender_matching_a_contact_with_an_embedded_photo_gets_it_as_the_avatar(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        avatar_addressbook: dict[str, Any],
    ) -> None:
        sender_email = "photo-sender@example.com"
        # A real GIF, not fabricated bytes -- the browser must actually
        # decode this as an image for the avatar to render it rather than
        # falling back to initials on a load error.
        photo_data_url = _TINY_GIF
        created = api_client.post(
            "/api/contacts",
            json={
                "addressbook_id": avatar_addressbook["id"],
                "summary": "Photo Sender",
                "emails": [{"email": sender_email}],
                "photo_data_url": photo_data_url,
            },
        )
        assert created.status_code == 201, created.text

        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI avatar photo test",
            "<p>hello</p>",
            sender=f"Photo Sender <{sender_email}>",
        )

        page.goto(app_server)
        select_account(page, rendering_account)
        mail_row(page, target["id"]).click()

        expect(page.locator('[data-testid="email-body"]')).to_be_visible(timeout=15_000)
        header = page.locator('[data-testid="thread-message-header"]')
        photo = header.locator('[data-slot="avatar-image"]')
        expect(photo).to_be_visible(timeout=10_000)
        assert photo.get_attribute("src") == photo_data_url

    def test_a_matching_contacts_embedded_photo_shows_in_the_mail_list_row_too(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        rendering_account: dict[str, Any],
        inbox_folder: dict[str, Any],
        avatar_addressbook: dict[str, Any],
    ) -> None:
        """The reading pane isn't the only place a sender's avatar shows --
        the mail list row does too, read from one bulk photo index rather
        than a request of its own (see mail-list-item.tsx)."""
        sender_email = "list-photo-sender@example.com"
        created = api_client.post(
            "/api/contacts",
            json={
                "addressbook_id": avatar_addressbook["id"],
                "summary": "List Photo Sender",
                "emails": [{"email": sender_email}],
                "photo_data_url": _TINY_GIF,
            },
        )
        assert created.status_code == 201, created.text
        contact_id = created.json()["id"]

        target = _deliver_html(
            api_client, dovecot_endpoint, rendering_account["id"], rendering_account["email"],
            inbox_folder["id"], "UI list avatar photo test",
            "<p>hello</p>",
            sender=f"List Photo Sender <{sender_email}>",
        )

        page.goto(app_server)
        select_account(page, rendering_account)

        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        photo = row.locator('[data-slot="avatar-image"]')
        expect(photo).to_be_visible(timeout=10_000)
        # The index's photo_url for an embedded photo is this application's
        # own same-origin endpoint, not the raw data: URI -- keeps the bulk
        # index itself free of photo bytes (see get_photo_index).
        assert photo.get_attribute("src") == f"/api/contacts/{contact_id}/photo"

        streamed = api_client.get(f"/api/contacts/{contact_id}/photo")
        assert streamed.status_code == 200
        assert streamed.headers["content-type"] == "image/gif"
        assert streamed.content == base64.b64decode(_TINY_GIF.split(",", 1)[1])


_AVATAR_SCALE_MAILBOX_SIZE = 1200


@pytest.fixture(scope="module")
def avatar_scale_mailbox(postgres_url: str) -> tuple[str, str]:
    """A bare account with `_AVATAR_SCALE_MAILBOX_SIZE` messages spread
    across 50 distinct senders (`seed_large_mailbox`'s own convention),
    to prove the photo index against a mailbox large enough that the
    client never holds every row -- the same scale
    test_mail_selection_scale_ui.py uses for the same reason. Bridged
    through its own thread the way that module's `big_mailbox` fixture
    is, for the same reason: Playwright's sync API makes a loop appear
    "running" on the main thread for the duration of the browser
    fixtures, and asyncio.run() refuses to nest inside one already
    running."""

    async def _seed() -> tuple[str, str]:
        connection = DatabaseConnection(
            DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
        )
        await connection.init()
        try:
            async with connection.session() as session:
                account_id, _folder_id, _message_ids = await build_large_mailbox(
                    session, _AVATAR_SCALE_MAILBOX_SIZE,
                )
                await session.commit()
        finally:
            await connection.close()
        return str(account_id), f"large-mailbox-{account_id}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _seed()).result()


class TestAvatarPhotoIndexAtScale:
    """The photo index exists specifically so a virtualized list over many
    thousand messages never ties a network call to a row entering the
    viewport -- proven here against the scale the concern is about,
    rather than against the handful of rows the other tests in this
    module seed."""

    def test_one_request_serves_the_whole_scroll_and_virtualization_still_holds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        avatar_addressbook: dict[str, Any],
        avatar_scale_mailbox: tuple[str, str],
    ) -> None:
        account_id, account_name = avatar_scale_mailbox
        # seed_large_mailbox cycles 50 senders as sender{n}@example.com;
        # matching one of them puts a real photo on roughly 1 row in 50
        # without needing this test to control individual messages.
        created = api_client.post(
            "/api/contacts",
            json={
                "addressbook_id": avatar_addressbook["id"],
                "summary": "Scale Sender",
                "emails": [{"email": "sender0@example.com"}],
                "photo_data_url": _TINY_GIF,
            },
        )
        assert created.status_code == 201, created.text

        photo_index_requests: list[str] = []

        def _record(req: Request) -> None:
            if "/contacts/photo-index" in req.url:
                photo_index_requests.append(req.url)

        page.on("request", _record)

        page.goto(app_server)
        select_account(page, {"name": account_name})

        rows = page.locator('[data-testid="mail-row"]')
        expect(rows.first).to_be_visible(timeout=15_000)
        assert len(photo_index_requests) == 1, photo_index_requests

        # Scroll deep enough to unload and remount rows repeatedly -- the
        # same "hover the list, 30 wheel steps of 4000px" shape
        # test_mail_selection_scale_ui.py uses to prove virtualization
        # survives a long scroll (the hover matters: a wheel event
        # targets whatever is under the pointer, not the page as a
        # whole). One row in 50 matches the seeded contact, so a
        # virtualized DOM shows it only while that row happens to be
        # mounted -- checked after every step rather than assuming it
        # lands in view at either endpoint.
        rows.first.hover()
        photo = rows.locator('[data-slot="avatar-image"]').first
        seen_the_photo = photo.count() > 0
        for _ in range(30):
            page.mouse.wheel(0, 4000)
            seen_the_photo = seen_the_photo or photo.count() > 0
        mounted_at_bottom = rows.count()
        assert mounted_at_bottom < 60, (
            f"{mounted_at_bottom} mail rows mounted at once -- virtualization "
            "looks broken, not just a wide viewport"
        )
        assert len(photo_index_requests) == 1, (
            f"expected exactly one photo-index request for the whole scroll, got "
            f"{len(photo_index_requests)}: {photo_index_requests}"
        )

        for _ in range(30):
            page.mouse.wheel(0, -4000)
            seen_the_photo = seen_the_photo or photo.count() > 0
        expect(rows.first).to_be_visible(timeout=10_000)
        assert len(photo_index_requests) == 1, photo_index_requests
        assert seen_the_photo, "the seeded contact's photo never appeared across the scroll"
