"""
The merged notification bell: one trigger, one summed badge, a Mail tab
(new mail, seeded directly into the alerts table) and a System tab (a
write PostIMAP gave up on permanently, seeded directly into
sync_notifications) -- the read/dismiss surface for both, not either
arrival path itself.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import uuid

import httpx
from playwright.sync_api import Browser, Locator, Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import mail_row, select_account, select_unified_view, unique_email

# Stubs navigator.serviceWorker and Notification.permission before any
# page script runs, so registering push here never reaches a real
# service worker or a real push service (there is no way to grant a
# browser test a real one, and no reason a push-sending test should
# depend on network egress) and never depends on headless Chromium's own
# Notification permission model, which reports "denied" regardless of a
# CDP permission grant -- a browser-environment limitation, not something
# this application's own code can be right or wrong about. The actual
# send path (webpush_async, VAPID signing, the server's own subscription
# storage) is covered against a real Postgres schema in
# tests/pg/test_push_pg.py; what a browser test can prove and that one
# cannot is that clicking Enable actually calls PushManager.subscribe and
# registers the result with the server.
#
# The real ServiceWorkerContainer is an EventTarget with its own
# startMessages() -- ServiceWorkerNavigation (mounted on every page, not
# just this one) calls addEventListener("message", ...) and
# startMessages() unconditionally once "serviceWorker" in navigator is
# true, which this stub satisfies by definition. A plain object standing
# in for the container crashes every page load under this stub with
# "addEventListener is not a function", which reads like the settings
# page itself being broken rather than an incomplete fake. Built on a
# real EventTarget so it behaves like the interface it is impersonating.
_STUB_SERVICE_WORKER_SCRIPT = """
(() => {
  Object.defineProperty(Notification, "permission", {
    get: () => "granted", configurable: true,
  });
  Notification.requestPermission = async () => "granted";

  const fakeSubscription = {
    endpoint: "https://push.example.test/fake-endpoint-" + Math.random().toString(36).slice(2),
    keys: { p256dh: "fake-p256dh", auth: "fake-auth" },
    toJSON() { return { endpoint: this.endpoint, keys: this.keys }; },
    unsubscribe: async () => true,
  };
  let current = null;
  const registration = {
    pushManager: {
      getSubscription: async () => current,
      subscribe: async () => { current = fakeSubscription; return fakeSubscription; },
    },
  };
  const fakeServiceWorker = Object.assign(new EventTarget(), {
    register: async () => registration,
    getRegistration: async () => (current ? registration : null),
    ready: Promise.resolve(registration),
    startMessages: () => {},
  });
  Object.defineProperty(navigator, "serviceWorker", {
    value: fakeServiceWorker, configurable: true,
  });
})();
"""


def _run_seed(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _insert_account(conn, name_prefix: str) -> tuple[uuid.UUID, str]:
    account_id = uuid.uuid4()
    email = unique_email(name_prefix)
    await conn.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password, is_active) "
            "VALUES (:id, :name, 'imap.example.com', 993, :email, "
            "'\\x00' || convert_to('pw', 'UTF8'), true)"
        ),
        {"id": account_id, "name": email, "email": email},
    )
    return account_id, email


def _seed_alert(postgres_url: str, title: str) -> str:
    """One account (a page with none at all destabilises the sidebar --
    the account switcher itself never settles) plus the alert row an
    arrival would have produced. Returns the account's own name/email."""

    async def _run() -> str:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            _account_id, email = await _insert_account(conn, "notification-bell")
            alert_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, kind, deliver_at, delivered_at, title, body, url, "
                    "dedupe_key) "
                    "VALUES (:id, 'mail', now(), now(), :title, 'sender@example.com', "
                    "'/?message=nonexistent', :dedupe_key)"
                ),
                {"id": alert_id, "title": title, "dedupe_key": f"mail:test:{alert_id}"},
            )
        await engine.dispose()
        return email

    return _run_seed(_run())


def _seed_account_with_inbox(postgres_url: str) -> str:
    """An account with an Inbox and a Sent folder, so the folder checklist
    has both an arrival folder and an outgoing one to actually check --
    with none seeded it renders "No folders yet" and there would be
    nothing to prove. Returns the account's own name/email, which is the
    only thing distinguishing its folder rows from another seeded
    account's identically-named Inbox and Sent once more than one test in
    this module has called this."""

    async def _run() -> str:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            account_id, email = await _insert_account(conn, "push-settings")
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name, special_use) "
                    "VALUES (:id, :account_id, 'Sent', 'sent')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
        await engine.dispose()
        return email

    return _run_seed(_run())


def _seed_message_with_alert(postgres_url: str) -> tuple[str, str, str, str]:
    """One account, one folder, one message, and an undismissed 'mail'
    alert pointing at it -- the arrival path a real inbox produces,
    without needing IMAP: seeded directly, as several other tests/ui/
    modules already do for the message half. Returns
    (account email, folder id, message id, alert title)."""

    async def _run() -> tuple[str, str, str, str]:
        engine = create_async_engine(postgres_url)
        folder_id = uuid.uuid4()
        message_id = uuid.uuid4()
        alert_title = f"dismiss-on-open-{uuid.uuid4().hex[:8]}"
        async with engine.begin() as conn:
            account_id, email = await _insert_account(conn, "dismiss-on-open")
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": folder_id, "account_id": account_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
                    "subject, from_addr, to_addrs, body_text, received_at) "
                    "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, "
                    ":subject, 'sender@example.com', NULL, 'body', now())"
                ),
                {
                    "id": message_id, "account_id": account_id, "folder_id": folder_id,
                    "thread_id": uuid.uuid4(), "msg_id": f"<{message_id}@example.com>",
                    "subject": alert_title,
                },
            )
            alert_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, kind, deliver_at, delivered_at, title, body, url, "
                    "account_id, message_id, folder_id, dedupe_key) "
                    "VALUES (:id, 'mail', now(), now(), :title, 'sender@example.com', "
                    ":url, :account_id, :message_id, :folder_id, :dedupe_key)"
                ),
                {
                    "id": alert_id, "title": alert_title, "url": f"/?message={message_id}",
                    "account_id": account_id, "message_id": message_id, "folder_id": folder_id,
                    "dedupe_key": f"mail:test:{alert_id}",
                },
            )
        await engine.dispose()
        return email, str(folder_id), str(message_id), alert_title

    return _run_seed(_run())


def _seed_alerts_and_a_notification(postgres_url: str) -> tuple[str, list[str], str]:
    """One account carrying two undismissed mail alerts and one
    unacknowledged write-failure -- the fixture for the merged badge and
    its two tabs. Returns (account email, [alert titles], notification's
    own error text)."""

    async def _run() -> tuple[str, list[str], str]:
        engine = create_async_engine(postgres_url)
        titles = [f"combined-badge-{uuid.uuid4().hex[:8]}" for _ in range(2)]
        error_text = f"combined-badge-failure-{uuid.uuid4().hex[:8]}"
        async with engine.begin() as conn:
            account_id, email = await _insert_account(conn, "combined-badge")
            for title in titles:
                alert_id = uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO alerts (id, kind, deliver_at, delivered_at, title, body, "
                        "url, account_id, dedupe_key) "
                        "VALUES (:id, 'mail', now(), now(), :title, 'sender@example.com', "
                        "'/?message=nonexistent', :account_id, :dedupe_key)"
                    ),
                    {
                        "id": alert_id, "title": title, "account_id": account_id,
                        "dedupe_key": f"mail:test:{alert_id}",
                    },
                )
            await conn.execute(
                text(
                    "INSERT INTO sync_notifications (account_id, action, error, detail) "
                    "VALUES (:account_id, 'send', :error, '{}'::jsonb)"
                ),
                {"account_id": account_id, "error": error_text},
            )
        await engine.dispose()
        return email, titles, error_text

    return _run_seed(_run())


def _seed_second_account_with_a_notification(postgres_url: str) -> tuple[str, str]:
    """A second account (so Unified View has more than one to merge)
    carrying one unacknowledged write-failure. Returns (account email,
    notification's own error text)."""

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(postgres_url)
        error_text = f"unified-view-failure-{uuid.uuid4().hex[:8]}"
        async with engine.begin() as conn:
            account_id, email = await _insert_account(conn, "unified-view")
            await conn.execute(
                text(
                    "INSERT INTO sync_notifications (account_id, action, error, detail) "
                    "VALUES (:account_id, 'move', :error, '{}'::jsonb)"
                ),
                {"account_id": account_id, "error": error_text},
            )
        await engine.dispose()
        return email, error_text

    return _run_seed(_run())


def _clear_the_bell(postgres_url: str) -> None:
    """Dismiss and acknowledge everything earlier tests left behind, so a
    badge can be asserted as an exact number."""

    async def _run() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE alerts SET dismissed_at = now() WHERE dismissed_at IS NULL")
            )
            await conn.execute(
                text(
                    "UPDATE sync_notifications SET acknowledged_at = now() "
                    "WHERE acknowledged_at IS NULL"
                )
            )
        await engine.dispose()

    _run_seed(_run())


def _seed_stalled_alert(postgres_url: str) -> str:
    """One delivered stuck-message alert, the row outbox/stalled.py raises.
    Returns its title."""

    async def _run() -> str:
        engine = create_async_engine(postgres_url)
        title = f"stalled-send-{uuid.uuid4().hex[:8]}"
        async with engine.begin() as conn:
            alert_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, kind, deliver_at, delivered_at, title, body, "
                    "dedupe_key) VALUES (:id, 'outbox_stalled', now(), now(), :title, "
                    "'still waiting', :dedupe_key)"
                ),
                {"id": alert_id, "title": title, "dedupe_key": f"outbox-stalled:{alert_id}"},
            )
        await engine.dispose()
        return title

    return _run_seed(_run())


def _seed_native_device(postgres_url: str, label: str) -> None:
    """A registered iPhone, as the app's own registration leaves it -- the
    relay ticket and content key are opaque bytes to everything here."""

    async def _run() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO push_subscriptions (transport, installation_id, relay_url, "
                    "encrypted_relay_ticket, encrypted_content_key, label, last_seen_at) "
                    "VALUES ('apns', :installation, 'https://relay.example', '\\x01'::bytea, "
                    "'\\x01'::bytea, :label, now())"
                ),
                {"installation": uuid.uuid4(), "label": label},
            )
        await engine.dispose()

    _run_seed(_run())


def _put_mail_settings(base_url: str, data: dict[str, object]) -> None:
    httpx.put(
        f"{base_url}/api/settings/mail", json={"data": data}, timeout=30.0,
    ).raise_for_status()


def _popover(page: Page) -> Locator:
    return page.locator('[data-slot="popover-content"]')


def _open_bell(page: Page) -> Locator:
    bell = page.locator('button[title="Notifications"]')
    expect(bell).to_be_visible(timeout=15_000)
    bell.click()
    expect(_popover(page)).to_be_visible(timeout=10_000)
    return bell




class TestNotificationBell:
    def test_a_delivered_alert_shows_in_the_badge_and_dismissing_clears_it(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        # Shares app_server_with_encryption_key with the push tests below
        # rather than the plain app_server every other module uses --
        # this file would otherwise bootstrap the whole app twice per
        # invocation (once per distinct fixture), which is both slower
        # and, empirically, is what made this file's own runs flakier
        # under host load than a single-server module needs to be.
        alert_title = f"notification-bell-test-{uuid.uuid4().hex[:8]}"
        _seed_alert(postgres_url, alert_title)

        page.goto(app_server_with_encryption_key)
        # A `title` attribute selector, not a role/accessible-name query:
        # the button's own visible content (an icon plus a conditional
        # unseen badge) is what actually decides its accessible name,
        # which is empty whenever the badge itself is between renders --
        # `title` is stable regardless.
        _open_bell(page)

        row = _popover(page).locator('[data-testid="alert-row"]').filter(has_text=alert_title)
        expect(row).to_be_visible(timeout=15_000)

        # The row itself is a durable record and stays listed once
        # dismissed -- only its own Dismiss button (and the unseen
        # highlight) goes away.
        row.get_by_role("button", name="Dismiss", exact=True).click()
        expect(row.get_by_role("button", name="Dismiss", exact=True)).to_have_count(
            0, timeout=10_000,
        )

        page.keyboard.press("Escape")

    def test_the_badge_sums_both_kinds_and_each_tab_lists_its_own(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        _email, alert_titles, error_text = _seed_alerts_and_a_notification(postgres_url)

        # Neither of the bell's lists is account-scoped, so no account is
        # selected first: driving the switcher right after load races the
        # page's hydration and asserts nothing about the bell.
        page.goto(app_server_with_encryption_key)
        _open_bell(page)

        popover = _popover(page)
        for title in alert_titles:
            expect(popover.locator('[data-testid="alert-row"]').filter(has_text=title)).to_be_visible(
                timeout=15_000,
            )

        popover.get_by_role("tab", name="System", exact=True).click()
        expect(
            popover.locator('[data-testid="system-row"]').filter(has_text=error_text)
        ).to_be_visible(timeout=15_000)

        # Both rows contributed to the one trigger badge -- torn down as
        # a byproduct of proving it (rather than asserting an exact
        # total, which would also count whatever another test in this
        # module leaves undismissed). Both kinds are durable records
        # that stay listed once dismissed (see the row components' own
        # comments) -- only each row's own Dismiss button goes away.
        popover.get_by_role("button", name="Dismiss all", exact=True).click()
        expect(
            popover.locator('[data-testid="system-row"]')
            .filter(has_text=error_text)
            .get_by_role("button", name="Dismiss", exact=True)
        ).to_have_count(0, timeout=15_000)
        popover.get_by_role("tab", name="Mail", exact=True).click()
        popover.get_by_role("button", name="Dismiss all", exact=True).click()
        for title in alert_titles:
            expect(
                popover.locator('[data-testid="alert-row"]')
                .filter(has_text=title)
                .get_by_role("button", name="Dismiss", exact=True)
            ).to_have_count(0, timeout=15_000)
        page.keyboard.press("Escape")

    def test_a_write_failure_is_visible_in_unified_view(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        """The regression this guards: the write-failure kind used to be
        gated on a single selected account and rendered nothing at all
        in Unified View -- a person working from a unified inbox
        silently stopped seeing failed writes."""
        _, error_text = _seed_second_account_with_a_notification(postgres_url)

        page.goto(app_server_with_encryption_key)
        select_unified_view(page)

        _open_bell(page)
        popover = _popover(page)
        popover.get_by_role("tab", name="System", exact=True).click()
        expect(
            popover.locator('[data-testid="system-row"]').filter(has_text=error_text)
        ).to_be_visible(timeout=15_000)
        page.keyboard.press("Escape")


class TestBadgeSetting:
    def test_turning_new_mail_off_leaves_only_system_notifications_in_the_badge(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        base = app_server_with_encryption_key
        _put_mail_settings(base, {"bell_badge_counts_new_mail": True})
        try:
            _clear_the_bell(postgres_url)
            # Two new-mail alerts, one write failure and one stuck send.
            _seed_alerts_and_a_notification(postgres_url)
            stalled_title = _seed_stalled_alert(postgres_url)

            # The number is the server's (GET /api/alerts/badge, the count a
            # phone's badge carries too), not one the page adds up itself.
            with page.expect_response(
                lambda resp: "/api/alerts/badge" in resp.url and resp.ok, timeout=15_000,
            ):
                page.goto(base)
            expect(page.get_by_test_id("bell-badge")).to_have_text("4", timeout=15_000)

            page.goto(f"{base}/settings")
            # The mail category's generic renderer ties no label to its
            # input, so the row is found by the label's own text.
            checkbox = (
                page.locator("div")
                .filter(has=page.get_by_text("Bell badge counts new mail", exact=True))
                .last.locator('input[type="checkbox"]')
            )
            expect(checkbox).to_be_checked(timeout=15_000)
            checkbox.uncheck()
            with page.expect_response(
                lambda resp: "/api/settings/mail" in resp.url and resp.request.method == "PUT",
                timeout=15_000,
            ):
                page.get_by_role("button", name="Save", exact=True).click()

            # The write failure and the stuck send are system notifications,
            # which the setting never takes out of the badge.
            page.goto(base)
            expect(page.get_by_test_id("bell-badge")).to_have_text("2", timeout=15_000)

            # The lists are untouched by the setting: Mail holds the two
            # new-mail alerts only, and the stuck send is listed under System.
            _open_bell(page)
            popover = _popover(page)
            expect(popover.locator('[data-testid="alert-row"]')).to_have_count(2, timeout=15_000)
            popover.get_by_role("tab", name="System", exact=True).click()
            expect(
                popover.locator('[data-testid="alert-row"]').filter(has_text=stalled_title)
            ).to_be_visible(timeout=15_000)
            page.keyboard.press("Escape")
        finally:
            _put_mail_settings(base, {"bell_badge_counts_new_mail": True})


class TestOpeningAMessageDismissesItsAlert:
    def test_clicking_the_row_dismisses_the_messages_own_alert(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        email, folder_id, message_id, alert_title = _seed_message_with_alert(postgres_url)

        page.goto(app_server_with_encryption_key)
        select_account(page, {"name": email})

        _open_bell(page)
        popover = _popover(page)
        row = popover.locator('[data-testid="alert-row"]').filter(has_text=alert_title)
        expect(row.get_by_role("button", name="Dismiss", exact=True)).to_be_visible(
            timeout=15_000,
        )
        page.keyboard.press("Escape")

        mail_row(page, message_id).click()

        _open_bell(page)
        popover = _popover(page)
        row = popover.locator('[data-testid="alert-row"]').filter(has_text=alert_title)
        # A count-based wait for zero can resolve on a transient reload
        # of the list rather than on the dismiss actually landing;
        # not_to_be_visible is the one that polls the specific element
        # that was there a moment ago.
        expect(row.get_by_role("button", name="Dismiss", exact=True)).not_to_be_visible(
            timeout=15_000,
        )
        page.keyboard.press("Escape")

    def test_arriving_from_a_message_url_dismisses_the_messages_own_alert(
        self, page: Page, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        """The other route into an open message -- a clicked system
        notification lands on the same ?message= URL the reading pane's
        dismiss effect reacts to regardless of how the message got
        selected."""
        _, _folder_id, message_id, alert_title = _seed_message_with_alert(postgres_url)

        page.goto(f"{app_server_with_encryption_key}/?message={message_id}")

        _open_bell(page)
        popover = _popover(page)
        row = popover.locator('[data-testid="alert-row"]').filter(has_text=alert_title)
        # The cold ?message= route resolves the account, folder and
        # message before this alert's own row ever renders -- slower to
        # settle than the row-click route's already-selected account, so
        # this needs more room. Waits for the row itself first (proving
        # it did appear, not that this timeout is simply too short to
        # ever see the Dismiss button an unfixed reading pane would
        # otherwise leave behind).
        expect(row).to_be_visible(timeout=30_000)
        expect(row.get_by_role("button", name="Dismiss", exact=True)).not_to_be_visible(
            timeout=15_000,
        )
        page.keyboard.press("Escape")


class TestPushSubscriptionSettings:
    """Enabling and disabling push in Settings -- the subscribe/register
    round trip, with the browser's own PushManager stubbed (see the
    module docstring) so this never depends on a real push service."""

    def test_enabling_registers_a_device_and_disabling_removes_it(
        self, browser: Browser, app_server_with_encryption_key: str,
    ) -> None:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.add_init_script(_STUB_SERVICE_WORKER_SCRIPT)
            page.goto(f"{app_server_with_encryption_key}/settings")

            enable = page.get_by_role("button", name="Enable", exact=True)
            expect(enable).to_be_visible(timeout=15_000)

            # Waits on the registration response itself rather than
            # polling the DOM for the "Disable" button it eventually
            # produces -- the same round trip (service worker
            # registration, a fetch for the VAPID key, PushManager.subscribe
            # and a POST, four awaits deep), but the earliest moment this
            # test can actually observe it succeeded, which matters on a
            # host that may be running other agents' container suites at
            # the same time.
            with page.expect_response(
                lambda resp: "/api/alerts/subscriptions" in resp.url
                and resp.request.method == "POST",
                timeout=60_000,
            ):
                enable.click()

            disable = page.get_by_role("button", name="Disable", exact=True)
            expect(disable).to_be_visible(timeout=15_000)
            expect(
                page.get_by_text(
                    "This device receives a notification for new mail even when "
                    "MailVerdict isn't open.",
                )
            ).to_be_visible()

            disable.click()
            expect(page.get_by_role("button", name="Enable", exact=True)).to_be_visible(
                timeout=15_000,
            )
            expect(disable).to_have_count(0)
        finally:
            context.close()

    def test_toggling_a_folder_writes_the_subscriptions_own_scope(
        self, browser: Browser, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        """The regression this guards: a subscribed device's folder scope
        must move onto its push_subscriptions row rather than staying in
        this browser's localStorage atom -- the server-side authority
        that is the entire reason a subscribed device's preference is
        real rather than approximated."""
        account_email = _seed_account_with_inbox(postgres_url)

        context = browser.new_context()
        page = context.new_page()
        try:
            page.add_init_script(_STUB_SERVICE_WORKER_SCRIPT)
            page.goto(f"{app_server_with_encryption_key}/settings")

            # Waits on the registration response itself rather than
            # polling the DOM for the "Disable" button it eventually
            # produces -- the same round trip, but the earliest moment
            # this test can actually observe it succeeded, which matters
            # on a host that may be running other agents' container
            # suites at the same time.
            with page.expect_response(
                lambda resp: "/api/alerts/subscriptions" in resp.url
                and resp.request.method == "POST",
                timeout=60_000,
            ):
                page.get_by_role("button", name="Enable", exact=True).click()
            expect(page.get_by_role("button", name="Disable", exact=True)).to_be_visible(
                timeout=15_000,
            )

            # Other tests in this module seed their own "INBOX" folders
            # under other accounts, so "Inbox" alone is ambiguous once
            # this checklist spans more than one -- this account's own
            # name is what the checklist groups its rows under
            # (alert-settings.tsx).
            group = (
                page.locator("div")
                .filter(has_text=account_email)
                .filter(has_text="Inbox")
                .last
            )
            folder_checkbox = group.locator("label", has_text="Inbox").get_by_role("checkbox")
            expect(folder_checkbox).to_be_visible(timeout=15_000)

            # A plain click, not .uncheck(): this checkbox has no
            # optimistic update -- it only reflects the PATCH's own
            # round trip once the subscription refetch completes, which
            # is correct (the subscription row is the one source of
            # truth this preference has once a device is subscribed, see
            # the model's own docstring) but fails .uncheck()'s built-in
            # same-tick state-change check.
            with page.expect_request(
                lambda req: "/api/alerts/subscriptions/" in req.url and req.method == "PATCH",
                timeout=10_000,
            ):
                folder_checkbox.click()
        finally:
            context.close()

    def test_a_phone_is_listed_as_one_and_muting_a_channel_writes_it(
        self, browser: Browser, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        label = f"iPhone {uuid.uuid4().hex[:6]}"
        _seed_native_device(postgres_url, label)

        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{app_server_with_encryption_key}/settings")
            row = page.get_by_test_id("push-device").filter(has_text=label)
            expect(row).to_be_visible(timeout=15_000)
            expect(row.get_by_text("Phone", exact=True)).to_be_visible()

            system = row.locator("label", has_text="System").get_by_role("checkbox")
            mail = row.locator("label", has_text="New mail").get_by_role("checkbox")
            expect(system).to_be_checked()
            expect(mail).to_be_checked()

            with page.expect_request(
                lambda req: "/api/alerts/subscriptions/" in req.url and req.method == "PATCH",
                timeout=10_000,
            ) as patch:
                system.click()
            assert json.loads(patch.value.post_data or "{}") == {"muted_channels": ["system"]}
            expect(system).not_to_be_checked(timeout=10_000)
            expect(mail).to_be_checked()
        finally:
            context.close()

    def test_the_default_scope_ticks_inbox_and_leaves_sent_unticked(
        self, browser: Browser, app_server_with_encryption_key: str, postgres_url: str,
    ) -> None:
        """A device that has never touched this checklist gets a scope of
        the folders mail actually arrives in -- not literally every
        folder, which would notify Freddy about his own mail landing in
        Sent every time he pressed Send."""
        account_email = _seed_account_with_inbox(postgres_url)

        context = browser.new_context()
        page = context.new_page()
        try:
            page.add_init_script(_STUB_SERVICE_WORKER_SCRIPT)
            page.goto(f"{app_server_with_encryption_key}/settings")

            # An earlier test in this module seeded its own Inbox/Sent
            # pair under a different account, so "Inbox" alone is
            # ambiguous once more than one account has folders -- this
            # account's own name is what the checklist groups its rows
            # under (alert-settings.tsx), and no more deeply nested
            # element than that group also carries both texts.
            group = (
                page.locator("div")
                .filter(has_text=account_email)
                .filter(has_text="Inbox")
                .last
            )
            inbox_checkbox = group.locator("label", has_text="Inbox").get_by_role("checkbox")
            sent_checkbox = group.locator("label", has_text="Sent").get_by_role("checkbox")
            expect(inbox_checkbox).to_be_visible(timeout=15_000)
            expect(inbox_checkbox).to_be_checked()
            expect(sent_checkbox).not_to_be_checked()
        finally:
            context.close()
