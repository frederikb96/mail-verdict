"""
The reading pane around one message or a whole conversation: newest-first
ordering, a header whose addresses copy rather than fold the message, a
link's destination shown on hover, dark mode that follows the canvas the
message is actually drawn on -- and, the part that matters most, that
opening mail from a sender nobody allowlisted makes no request anywhere.

That last one is proven in the browser's own network log against a real
listener every hostile reference in the fixture points at, never inferred
from the sanitizer's output string: the sanitizer, the client-side
sanitizer, the shadow root and the page's content security policy all
decide a piece of it, and only a real render shows what they add up to.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, Request, expect

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

_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
_TINY_GIF_DATA_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="

# How long an opened message is given to make any request it is going to
# make. The trusted half of the privacy test waits exactly this long for
# the same fixture's loads to arrive, so a pass there is what shows this
# wait is long enough for the untrusted half to mean anything.
_SETTLE_MS = 3_000


class _Beacon:
    """A real HTTP listener standing in for the sender's tracking host.

    Every hostile reference in a fixture points here, so a request that
    reaches it is exactly the signal a sender would receive -- whatever the
    browser's own request log does or does not show.
    """

    def __init__(self) -> None:
        self.hits: list[str] = []
        beacon = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- http.server's own naming
                beacon.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/gif")
                self.send_header("Content-Length", str(len(_GIF)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(_GIF)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def hits_for(self, token: str) -> list[str]:
        return [path for path in self.hits if token in path]

    def close(self) -> None:
        self._server.shutdown()


@pytest.fixture(scope="module")
def beacon() -> Iterator[_Beacon]:
    server = _Beacon()
    yield server
    server.close()


@pytest.fixture(scope="module")
def reader_account(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, Any]:
    email = unique_email("reader")
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
def inbox(api_client: httpx.Client, reader_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_folder(api_client, str(reader_account["id"]), "INBOX")


def _deliver(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account: dict[str, Any],
    inbox_id: str,
    *,
    subject: str,
    body: str,
    sender: str = "sender@example.com",
    to: str | None = None,
    content_type: str = "text/html; charset=utf-8",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deliver one message to the account over LMTP and wait for it in INBOX."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    envelope_sender = sender.split("<")[-1].rstrip(">") if "<" in sender else sender
    raw = build_eml(
        sender=sender,
        recipient=to or account["email"],
        subject=subject,
        body=body,
        message_id=message_id or f"<{uuid.uuid4()}@example.com>",
        in_reply_to=in_reply_to,
        content_type=content_type,
        extra_headers=extra_headers,
    )
    deliver_message(raw, host, lmtp_port, sender=envelope_sender, recipient=account["email"])

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account['id']}/messages", params={"folder_id": inbox_id},
        )
        assert resp.status_code == 200, resp.text
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_find, description=f"{subject!r} synced into INBOX")


def _open(
    page: Page, app_server: str, account: dict[str, Any], mail_id: str, *, cold: bool = False,
) -> None:
    """Open one message. `cold` drops the persisted query cache first: an
    allowlist change made through the API rather than the reading pane's own
    control invalidates nothing, so the thread body cached from the previous
    open would otherwise be restored and shown as still fresh."""
    if cold:
        page.evaluate("localStorage.clear()")
    page.goto(app_server)
    select_account(page, account)
    mail_row(page, mail_id).click()
    expect(page.locator('[data-testid="email-body"]').first).to_be_visible(timeout=15_000)


class TestConversationOrder:
    def test_a_three_message_thread_reads_newest_first(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
    ) -> None:
        token = uuid.uuid4().hex[:8]
        ids: list[str] = []
        previous: str | None = None
        for n in (1, 2, 3):
            message_id = f"<order-{token}-{n}@example.com>"
            delivered = _deliver(
                api_client, dovecot_endpoint, reader_account, inbox["id"],
                subject=f"Order {token} part {n}", body=f"Order body {n}",
                content_type="text/plain; charset=utf-8",
                message_id=message_id, in_reply_to=previous,
            )
            ids.append(delivered["id"])
            previous = message_id
            # received_at is the delivery's own timestamp -- a gap keeps
            # the three strictly ordered rather than relying on a tie-break.
            time.sleep(1.1)

        thread = wait_for(
            lambda: (
                resp.json()["messages"]
                if (resp := api_client.get(f"/api/messages/{ids[0]}/thread")).status_code == 200
                and len(resp.json()["messages"]) == 3
                else None
            ),
            description="all three messages threaded together",
        )
        assert [m["id"] for m in thread] == ids, "the API itself should stay ascending"

        _open(page, app_server, reader_account, ids[2])
        messages = page.locator('[data-testid="thread-message"]')
        expect(messages).to_have_count(3, timeout=15_000)
        rendered = [messages.nth(i).get_attribute("data-message-id") for i in range(3)]
        assert rendered == list(reversed(ids)), f"rendered order {rendered}, expected newest first"


class TestMessageHeader:
    def test_addresses_copy_and_only_the_blank_area_folds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
    ) -> None:
        target = _deliver(
            api_client, dovecot_endpoint, reader_account, inbox["id"],
            subject=f"Header copy {uuid.uuid4().hex[:8]}",
            body="<p>Header body</p>",
            sender="Sam Sender <sam@example.com>",
            to="Ann Example <ann@example.com>, bob@example.com",
            extra_headers={"Cc": "carol@example.com"},
        )
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=app_server)
        _open(page, app_server, reader_account, target["id"])

        header = page.locator('[data-testid="thread-message-header"]')
        body = page.locator('[data-testid="email-body"]')

        def clipboard() -> str:
            return page.evaluate("navigator.clipboard.readText()")

        def click_and_expect_copy(target_locator: Any, expected: str) -> None:
            page.evaluate("navigator.clipboard.writeText('')")
            target_locator.click()
            expect(body).to_be_visible()
            wait_for(lambda: clipboard() == expected or None, timeout_s=5,
                     description=f"clipboard to read {expected!r}")

        click_and_expect_copy(header.locator('[data-testid="thread-message-sender"]'),
                              "sam@example.com")
        recipients = header.locator('[data-testid="thread-message-recipient"]')
        expect(recipients).to_have_count(3)
        click_and_expect_copy(recipients.nth(1), "bob@example.com")
        click_and_expect_copy(header.get_by_role("button", name="Copy all To addresses"),
                              "ann@example.com, bob@example.com")
        click_and_expect_copy(header.get_by_role("button", name="Copy all Cc addresses"),
                              "carol@example.com")

        # Neither of these copies anything, and neither folds the message.
        header.locator('[data-slot="avatar"]').first.click()
        expect(body).to_be_visible()
        header.locator('[data-testid="thread-message-date"]').click()
        expect(body).to_be_visible()

        # The header's own padding is blank -- clicking there is a fold.
        header.click(position={"x": 3, "y": 3})
        expect(body).not_to_be_visible(timeout=5_000)
        page.locator('[data-testid="thread-message-header"]').click(position={"x": 3, "y": 3})
        expect(body).to_be_visible(timeout=5_000)


class TestLinkDestination:
    def test_hovering_a_link_shows_where_it_goes(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
    ) -> None:
        destination = "https://destination.example/landing?campaign=1"
        target = _deliver(
            api_client, dovecot_endpoint, reader_account, inbox["id"],
            subject=f"Link hover {uuid.uuid4().hex[:8]}",
            body=f'<p>Before <a href="{destination}">Visit the site</a> after</p>',
        )
        _open(page, app_server, reader_account, target["id"])

        status = page.locator('[data-testid="link-status"]')
        page.locator('[data-testid="email-body"] a', has_text="Visit the site").hover()
        expect(status).to_have_text(destination, timeout=5_000)

        page.mouse.move(1, 1)
        expect(status).not_to_be_visible(timeout=5_000)


def _hostile_document(base: str, port: int, token: str) -> str:
    """Every way a message's markup could make the reader's browser fetch
    something, each pointing at its own path on the beacon so a request
    names the vector that made it -- as a whole document, the shape real
    mail arrives in, with its stylesheet and link/meta/base tags in the
    head where a sender would put them."""

    def u(name: str) -> str:
        return f"{base}/{token}/{name}"

    head = (
        "<style>"
        f'@import url("{u("stylesheet-import")}");'
        f".sb{{background-image:url({u('stylesheet-background')});width:10px;height:10px}}"
        f'.sis{{background-image:image-set("{u("stylesheet-image-set")}" 1x);'
        "width:10px;height:10px}"
        f'.sv{{--u:"{u("stylesheet-var")}";background-image:image-set(var(--u) 1x);'
        "width:10px;height:10px}"
        f"@font-face{{font-family:BeaconFont;src:url({u('stylesheet-font')})}}"
        ".sf{font-family:BeaconFont}"
        f"@media screen{{.sm{{background-image:url({u('stylesheet-media')});"
        "width:10px;height:10px}}"
        f".sc::before{{content:url({u('stylesheet-content')})}}"
        "</style>"
        f'<link rel="stylesheet" href="{u("link-stylesheet")}">'
        f'<link rel="prefetch" href="{u("link-prefetch")}">'
        f'<link rel="preload" as="image" href="{u("link-preload")}">'
        f'<link rel="icon" href="{u("link-icon")}">'
        f'<link rel="dns-prefetch" href="{base}">'
        f'<meta http-equiv="refresh" content="0;url={u("meta-refresh")}">'
        f'<base href="{base}/{token}/base/">'
    )
    body = "".join(_hostile_body_vectors(u, port, token).values())
    return (
        f"<!DOCTYPE html><html><head>{head}</head>"
        f'<body background="{u("body-background")}"><p>Hostile fixture</p>{body}'
        '<div class="sb">1</div><div class="sis">2</div><div class="sv">3</div>'
        '<p class="sf">font</p><div class="sm">4</div><div class="sc">5</div>'
        "</body></html>"
    )


def _hostile_body_vectors(u: Any, port: int, token: str) -> dict[str, str]:
    return {
        "img-src": f'<img src="{u("img-src")}" width="10" height="10">',
        "img-srcset": f'<img src="{_TINY_GIF_DATA_URL}" srcset="{u("img-srcset")} 2x">',
        "picture": (
            f'<picture><source srcset="{u("picture-source")}">'
            f'<img src="{u("picture-img")}"></picture>'
        ),
        "protocol-relative": f'<img src="//127.0.0.1:{port}/{token}/protocol-relative">',
        "table-background": (
            f'<table background="{u("table-background")}"><tr><td>t</td></tr></table>'
        ),
        "td-background": f'<table><tr><td background="{u("td-background")}">t</td></tr></table>',
        "style-background": (
            f'<div style="background-image:url({u("style-background")});'
            'width:10px;height:10px">s</div>'
        ),
        "style-image-set": (
            f"<div style=\"background-image:image-set('{u('style-image-set')}' 1x);"
            'width:10px;height:10px">s</div>'
        ),
        "style-webkit-image-set": (
            f"<div style=\"background-image:-webkit-image-set('{u('style-webkit-image-set')}' 1x);"
            'width:10px;height:10px">s</div>'
        ),
        "style-list-style": (
            f'<ul style="list-style-image:url({u("style-list-style")})"><li>l</li></ul>'
        ),
        "style-border-image": (
            f'<div style="border:5px solid;border-image:url({u("style-border-image")}) 30">b</div>'
        ),
        "media": (
            f'<video poster="{u("video-poster")}" src="{u("video-src")}"></video>'
            f'<audio src="{u("audio-src")}" autoplay></audio>'
        ),
        "frames": (
            f'<iframe src="{u("iframe")}"></iframe>'
            f'<object data="{u("object")}"></object>'
            f'<embed src="{u("embed")}">'
        ),
        "svg": f'<svg><image href="{u("svg-image")}" width="10" height="10"/></svg>',
        "input": f'<input type="image" src="{u("input-image")}">',
        "relative-to-base": '<img src="relative.gif">',
        "anchor": f'<a href="{u("a-href")}" ping="{u("a-ping")}">A tracked link</a>',
    }


# What an allowlisted sender's own mail is expected to load -- every
# reference the sanitizer gates on trust rather than removing outright.
# Everything else in the fixture stays blocked for a trusted sender too,
# since it is active content or escapes the message's box rather than a
# tracking question. The one exception is @font-face: restored like any
# other trusted reference, but a browser does not apply a font declared
# inside a shadow root, so it never loads for anyone.
_TRUSTED_LOADS = frozenset({
    "img-src", "picture-img", "table-background", "td-background",
    "style-background", "style-image-set", "style-webkit-image-set",
    "style-list-style", "style-border-image",
    "stylesheet-background", "stylesheet-image-set", "stylesheet-var",
    "stylesheet-media", "stylesheet-content",
})


class TestOpeningMailSendsNothing:
    def test_an_untrusted_sender_learns_nothing_and_a_trusted_one_loads(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
        beacon: _Beacon,
    ) -> None:
        token = f"t{uuid.uuid4().hex[:10]}"
        sender = f"tracker-{token}@beacon.example"
        document = _hostile_document(beacon.base, beacon.port, token)
        outbox_before = api_client.get("/api/outbox")
        assert outbox_before.status_code == 200, outbox_before.text

        target = _deliver(
            api_client, dovecot_endpoint, reader_account, inbox["id"],
            subject=f"Hostile fixture {token}", body=document, sender=sender,
            extra_headers={
                "Disposition-Notification-To": sender,
                "Return-Receipt-To": sender,
                "List-Unsubscribe": f"<{beacon.base}/{token}/list-unsubscribe>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )

        foreign: list[str] = []

        def _record(req: Request) -> None:
            # data: and about: never leave the browser -- about:blank is what
            # every neutralised reference is rewritten to.
            if not req.url.startswith((app_server, "data:", "about:")):
                foreign.append(req.url)

        page.on("request", _record)

        _open(page, app_server, reader_account, target["id"])
        body = page.locator('[data-testid="email-body"]')
        expect(body.get_by_text("Hostile fixture")).to_be_visible(timeout=15_000)
        body.get_by_text("A tracked link").hover()
        page.wait_for_timeout(_SETTLE_MS)

        assert beacon.hits_for(token) == [], (
            f"the sender's host received {beacon.hits_for(token)} from a message nobody trusted"
        )
        assert foreign == [], f"the browser reached outside the application: {foreign}"
        outbox_after = api_client.get("/api/outbox")
        assert len(outbox_after.json()) == len(outbox_before.json()), (
            "opening the message queued mail -- a read receipt would reach the sender"
        )

        resp = api_client.post(
            f"/api/accounts/{reader_account['id']}/image-exceptions",
            json={"type": "sender", "value": sender},
            timeout=90.0,
        )
        assert resp.status_code == 201, resp.text

        _open(page, app_server, reader_account, target["id"], cold=True)
        expect(body.get_by_text("Hostile fixture")).to_be_visible(timeout=15_000)
        page.wait_for_timeout(_SETTLE_MS)

        loaded = {path.split("/")[-1] for path in beacon.hits_for(token)}
        missing = _TRUSTED_LOADS - loaded
        assert not missing, f"an allowlisted sender's content did not load: {sorted(missing)}"
        unexpected = loaded - _TRUSTED_LOADS
        assert not unexpected, (
            f"trust relaxes tracking only, yet these loaded as well: {sorted(unexpected)}"
        )


_DARK_AWARE_HTML = (
    "<style>@media (prefers-color-scheme: dark) {{"
    " .msg {{ color: #eeeeee !important }}"
    " .logo {{ background-image: url({logo}) }} }}</style>"
    '<p class="msg" style="color:#111111">Dark aware message</p>'
    '<div class="logo" style="width:10px;height:10px"></div>'
)


class TestSenderDarkMode:
    """A message that ships its own dark styles gets them whenever it is drawn
    on the dark canvas -- which is decided by this application's theme, not by
    whatever the operating system happens to prefer -- and never when it is
    drawn on the light one. Trust only decides whether the dark variant's
    images load, the same way it does for every other image."""

    def _open_in(
        self, browser: Browser, app_server: str, account: dict[str, Any], mail_id: str,
        *, app_theme: str, system_scheme: str,
    ) -> tuple[Any, Page]:
        context = browser.new_context(color_scheme=system_scheme)  # type: ignore[arg-type]
        context.add_init_script(f"localStorage.setItem('theme', '{app_theme}')")
        page = context.new_page()
        _open(page, app_server, account, mail_id)
        return context, page

    def test_dark_theme_applies_the_senders_own_dark_styles(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
        beacon: _Beacon,
    ) -> None:
        token = f"d{uuid.uuid4().hex[:10]}"
        sender = f"dark-{token}@example.com"
        target = _deliver(
            api_client, dovecot_endpoint, reader_account, inbox["id"],
            subject=f"Dark aware {token}", sender=sender,
            body=_DARK_AWARE_HTML.format(logo=f"{beacon.base}/{token}/dark-logo"),
        )

        # The application is dark while the operating system is light: the
        # sender's dark rules must follow the canvas, not the system.
        context, page = self._open_in(
            browser, app_server, reader_account, target["id"],
            app_theme="dark", system_scheme="light",
        )
        try:
            body = page.locator('[data-testid="email-body"]')
            expect(page.get_by_role(
                "button", name="Switch this message to light mode", exact=True,
            )).to_be_visible(timeout=10_000)
            expect(body.locator(".msg")).to_have_css("color", "rgb(238, 238, 238)")
            page.wait_for_timeout(_SETTLE_MS)
            assert beacon.hits_for(token) == [], "an untrusted sender's dark logo loaded"

            resp = api_client.post(
                f"/api/accounts/{reader_account['id']}/image-exceptions",
                json={"type": "sender", "value": sender},
                timeout=90.0,
            )
            assert resp.status_code == 201, resp.text
            _open(page, app_server, reader_account, target["id"], cold=True)
            expect(body.locator(".msg")).to_have_css("color", "rgb(238, 238, 238)")
            wait_for(lambda: beacon.hits_for(token) or None, timeout_s=10,
                     description="the trusted sender's dark logo to load")
        finally:
            context.close()

    def test_light_theme_keeps_the_senders_dark_styles_off(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        reader_account: dict[str, Any],
        inbox: dict[str, Any],
        beacon: _Beacon,
    ) -> None:
        token = f"l{uuid.uuid4().hex[:10]}"
        target = _deliver(
            api_client, dovecot_endpoint, reader_account, inbox["id"],
            subject=f"Light theme {token}",
            body=_DARK_AWARE_HTML.format(logo=f"{beacon.base}/{token}/dark-logo"),
        )
        # The operating system prefers dark, the application is light: a
        # message on the light canvas must not pick up its dark rules, or its
        # light text lands on white.
        context, page = self._open_in(
            browser, app_server, reader_account, target["id"],
            app_theme="light", system_scheme="dark",
        )
        try:
            body = page.locator('[data-testid="email-body"]')
            expect(page.get_by_role(
                "button", name="Enable dark message mode", exact=True,
            )).to_be_visible(timeout=10_000)
            expect(body.locator(".msg")).to_have_css("color", "rgb(17, 17, 17)")
        finally:
            context.close()
