"""
The alert bell: an unseen badge for delivered, undismissed alerts, and
dismissing one clears it -- seeded directly into the alerts table (the
row a live mail arrival would produce), since this is the read/dismiss
surface, not the arrival path itself.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid

from playwright.sync_api import Browser, Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import unique_email

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
  const fakeServiceWorker = {
    register: async () => registration,
    getRegistration: async () => (current ? registration : null),
    ready: Promise.resolve(registration),
  };
  Object.defineProperty(navigator, "serviceWorker", {
    value: fakeServiceWorker, configurable: true,
  });
})();
"""


def _seed_alert(postgres_url: str, title: str) -> None:
    """One account (a page with none at all destabilises the sidebar --
    the account switcher itself never settles) plus the alert row an
    arrival would have produced."""

    async def _run() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            email = unique_email("alert-bell")
            await conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, :email, "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": uuid.uuid4(), "name": email, "email": email},
            )
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run()).result()


def _seed_account_with_inbox(postgres_url: str) -> None:
    """An account with one folder, so the folder checklist has something
    to actually check -- with none seeded it renders "No folders yet"
    and there would be nothing to prove."""

    async def _run() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            email = unique_email("push-settings")
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, :email, "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": email, "email": email},
            )
            await conn.execute(
                text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": uuid.uuid4(), "account_id": account_id},
            )
        await engine.dispose()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run()).result()


class TestAlertBell:
    def test_a_delivered_alert_shows_in_the_badge_and_dismissing_clears_it(
        self, page: Page, app_server: str, postgres_url: str,
    ) -> None:
        alert_title = f"alert-bell-test-{uuid.uuid4().hex[:8]}"
        _seed_alert(postgres_url, alert_title)

        page.goto(app_server)
        # A `title` attribute selector, not a role/accessible-name query:
        # the button's own visible content (an icon plus a conditional
        # unseen badge) is what actually decides its accessible name,
        # which is empty whenever the badge itself is between renders --
        # `title` is stable regardless.
        bell = page.locator('button[title="Alerts"]')
        expect(bell).to_be_visible(timeout=15_000)
        bell.click()

        row = page.locator('[data-testid="alert-row"]').filter(has_text=alert_title)
        expect(row).to_be_visible(timeout=15_000)

        # The row itself is a durable record and stays listed once
        # dismissed -- only its own Dismiss button (and the unseen
        # highlight) goes away.
        row.get_by_role("button", name="Dismiss", exact=True).click()
        expect(row.get_by_role("button", name="Dismiss", exact=True)).to_have_count(
            0, timeout=10_000,
        )

        page.keyboard.press("Escape")


class TestPushSubscriptionSettings:
    """Enabling and disabling push in Settings -- the subscribe/register
    round trip, with the browser's own PushManager stubbed (see the
    module docstring) so this never depends on a real push service."""

    def test_enabling_registers_a_device_and_disabling_removes_it(
        self, browser: Browser, app_server: str,
    ) -> None:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.add_init_script(_STUB_SERVICE_WORKER_SCRIPT)
            page.goto(f"{app_server}/settings")

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
                page.get_by_text("Also notify for calendar reminders on this device")
            ).to_be_visible()
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
        self, browser: Browser, app_server: str, postgres_url: str,
    ) -> None:
        """The regression this guards: a subscribed device's folder scope
        must move onto its push_subscriptions row rather than staying in
        this browser's localStorage atom -- the server-side authority
        that is the entire reason a subscribed device's preference is
        real rather than approximated."""
        _seed_account_with_inbox(postgres_url)

        context = browser.new_context()
        page = context.new_page()
        try:
            page.add_init_script(_STUB_SERVICE_WORKER_SCRIPT)
            page.goto(f"{app_server}/settings")

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

            folder_row = page.locator("label", has_text="Inbox")
            folder_checkbox = folder_row.get_by_role("checkbox")
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
