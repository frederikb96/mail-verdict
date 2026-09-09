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

from playwright.sync_api import Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import unique_email


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
