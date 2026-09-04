"""
Selection against a mailbox large enough that the client never holds
every row -- the scale the predicate/scope design exists for. Seeded
directly into the mirror via tests/setup/large_mailbox.py rather than
delivered over LMTP, the same way tests/pg's own bulk tests do: LMTP
delivery at this scale would dominate the test's own runtime for no
reason related to what's under test here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import uuid

import pytest
from playwright.sync_api import Page, Request, expect

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from tests.setup.large_mailbox import build_large_mailbox, seed_large_mailbox
from tests.ui.helpers import mail_row, select_account

# Comfortably past EAGER_REFETCH_MAX_PAGES (3 pages / ~150 rows) and past
# what a handful of scroll gestures would reach by accident, so "scroll
# far and back" genuinely exercises rows the client unloaded and reloaded.
_MAILBOX_SIZE = 1200


@pytest.fixture(scope="module")
def big_mailbox(postgres_url: str) -> tuple[str, str, list[str]]:
    """A bare account with one folder holding `_MAILBOX_SIZE` messages,
    account_id/folder_id/message_ids all as strings. Bridged through its
    own thread the same way app_server bridges migrations: Playwright's
    sync API makes a loop appear "running" on the main thread for the
    duration of the browser fixtures, and asyncio.run() refuses to nest
    inside one that's already running."""

    async def _seed() -> tuple[str, str, list[str]]:
        connection = DatabaseConnection(
            DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
        )
        await connection.init()
        try:
            async with connection.session() as session:
                account_id, folder_id, message_ids = await build_large_mailbox(
                    session, _MAILBOX_SIZE,
                )
                await session.commit()
        finally:
            await connection.close()
        return str(account_id), str(folder_id), [str(m) for m in message_ids]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _seed()).result()


def _open_big_mailbox(page: Page, app_server: str, account_id: str) -> None:
    page.goto(app_server)
    select_account(page, {"name": f"large-mailbox-{account_id}"})


class TestLargeMailboxSelectionUi:
    """One seeded mailbox, reused by every test below (module-scoped) --
    each test starts from a fresh page load, so the selection itself
    never carries over between them."""

    def test_select_all_deselect_two_scroll_far_and_back_keeps_them_unticked(
        self, page: Page, app_server: str, big_mailbox: tuple[str, str, list[str]],
    ) -> None:
        """The one test that proves the whole design: a predicate
        selection plus two exclusions survives rows unmounting and
        remounting during a long scroll, because membership is
        re-derived from the predicate on every render rather than held
        per row."""
        account_id, _folder_id, message_ids = big_mailbox
        # Newest-first: the list's own top row is the last-inserted message.
        top_id, second_id, third_id = message_ids[-1], message_ids[-2], message_ids[-3]

        _open_big_mailbox(page, app_server, account_id)
        top_row = mail_row(page, top_id)
        expect(top_row).to_be_visible(timeout=15_000)

        # Predicate selection ("select every message") is only offered on
        # a flat list -- a threaded row is a conversation, not a message,
        # and this view defaults to threaded.
        page.get_by_role("switch", name="Group by conversation").click()

        top_row.hover()
        top_row.get_by_role("checkbox").click()
        page.get_by_role("button", name="Select", exact=True).click()
        page.get_by_role("menuitem", name="Every message in this folder", exact=True).click()

        expect(page.get_by_text(f"{_MAILBOX_SIZE} selected", exact=True)).to_be_visible(
            timeout=15_000,
        )

        second_row = mail_row(page, second_id)
        third_row = mail_row(page, third_id)
        # top_row's own checkbox click above happened before the predicate
        # existed (it was what started the selection) -- minting the
        # predicate resets included/excluded, so top_row is now selected
        # via the predicate itself, not an explicit include. Toggling it
        # again here is what actually excludes it.
        top_row.get_by_role("checkbox").click()
        second_row.get_by_role("checkbox").click()
        expect(third_row.get_by_role("checkbox")).to_be_checked()
        expect(top_row.get_by_role("checkbox")).not_to_be_checked()
        expect(second_row.get_by_role("checkbox")).not_to_be_checked()
        expect(page.get_by_text(f"{_MAILBOX_SIZE - 2} selected", exact=True)).to_be_visible()

        # Scroll far enough to unmount everything currently rendered, then
        # all the way back. Wheel rather than "j" keypresses -- far
        # cheaper for this distance, and virtua's onScroll listens to the
        # DOM's scroll event regardless of what triggered it.
        list_area = page.locator('[data-testid="mail-row"]').first
        list_area.hover()
        for _ in range(30):
            page.mouse.wheel(0, 4000)
        expect(top_row).not_to_be_visible()

        for _ in range(30):
            page.mouse.wheel(0, -4000)
        expect(top_row).to_be_visible(timeout=15_000)

        expect(top_row.get_by_role("checkbox")).not_to_be_checked()
        expect(second_row.get_by_role("checkbox")).not_to_be_checked()
        expect(third_row.get_by_role("checkbox")).to_be_checked()

    def test_scrolling_does_not_grow_the_persisted_query_cache(
        self, page: Page, app_server: str, big_mailbox: tuple[str, str, list[str]],
    ) -> None:
        """The mail list's own infinite query is excluded from
        persistence -- scrolling deep into it must never grow what's
        written to localStorage under the app's cache key."""
        account_id, _folder_id, _message_ids = big_mailbox

        _open_big_mailbox(page, app_server, account_id)
        expect(page.locator('[data-testid="mail-row"]').first).to_be_visible(timeout=15_000)

        list_area = page.locator('[data-testid="mail-row"]').first
        list_area.hover()
        for _ in range(25):
            page.mouse.wheel(0, 4000)
        # The persister throttles writes to localStorage -- give the
        # trailing edge of that throttle a moment to fire before reading.
        page.wait_for_timeout(1500)

        cache_raw = page.evaluate(
            "() => window.localStorage.getItem('mail-verdict-query-cache')"
        )
        assert cache_raw is not None, "expected the query cache key to exist in localStorage"
        assert '"mails"' not in cache_raw, (
            "the mail list's infinite query was persisted -- it should be excluded"
        )

    def test_arriving_mail_issues_a_bounded_number_of_requests_when_scrolled_deep(
        self,
        page: Page,
        app_server: str,
        postgres_url: str,
        big_mailbox: tuple[str, str, list[str]],
    ) -> None:
        """A single new message must not turn a deeply-scrolled list into
        a refetch of every page it has ever loaded -- TanStack's infinite
        query refetches every already-fetched page on a non-directional
        invalidation, sequentially, with no cap. Scroll well past the
        bounded helper's page threshold, then insert one more message
        directly (the same postimap_events NOTIFY a real sync fires) and
        count requests to the list endpoint."""
        account_id, folder_id, _message_ids = big_mailbox

        _open_big_mailbox(page, app_server, account_id)
        expect(page.locator('[data-testid="mail-row"]').first).to_be_visible(timeout=15_000)

        list_area = page.locator('[data-testid="mail-row"]').first
        list_area.hover()
        for _ in range(40):
            page.mouse.wheel(0, 4000)
        # Let pagination settle -- fetchNextPage triggers a bit behind the
        # scroll itself, and the count below must not include this catch-up.
        page.wait_for_timeout(2000)

        list_requests: list[Request] = []
        page.on(
            "request",
            lambda req: list_requests.append(req) if "/messages" in req.url else None,
        )

        async def _deliver_one_more() -> None:
            connection = DatabaseConnection(
                DatabaseConfig(
                    url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0,
                )
            )
            await connection.init()
            try:
                async with connection.session() as session:
                    await seed_large_mailbox(
                        session, uuid.UUID(account_id), uuid.UUID(folder_id), 1,
                        uid_start=_MAILBOX_SIZE + 100,
                    )
                    await session.commit()
            finally:
                await connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, _deliver_one_more()).result()

        # Give the NOTIFY -> SSE -> client round trip a few seconds, then
        # a further quiet window to catch any delayed page refetches.
        page.wait_for_timeout(5000)

        assert len(list_requests) <= 3, (
            f"a single arriving message issued {len(list_requests)} requests to "
            f"the mail list endpoint on a deeply-scrolled folder: "
            f"{[r.url for r in list_requests]}"
        )

    def test_bulk_action_over_the_whole_mailbox_stays_bounded(
        self, page: Page, app_server: str, big_mailbox: tuple[str, str, list[str]],
    ) -> None:
        """PostIMAP's own trigger family fires one live-update event per
        row -- a bulk write over the whole mailbox emits `_MAILBOX_SIZE`
        of them in a burst far tighter than handling even one of them
        takes. Marks the whole predicate-selected mailbox read through
        the bulk panel (a single server-resolved statement, never an
        enumerated id list) and counts requests from the moment it fires
        through a quiet settling window -- unbounded per-event handling
        would turn this one click into roughly `_MAILBOX_SIZE` of them."""
        account_id, _folder_id, message_ids = big_mailbox
        top_id = message_ids[-1]

        _open_big_mailbox(page, app_server, account_id)
        top_row = mail_row(page, top_id)
        expect(top_row).to_be_visible(timeout=15_000)

        page.get_by_role("switch", name="Group by conversation").click()
        top_row.hover()
        top_row.get_by_role("checkbox").click()
        page.get_by_role("button", name="Select", exact=True).click()
        page.get_by_role("menuitem", name="Every message in this folder", exact=True).click()
        # Not pinned to _MAILBOX_SIZE exactly: an earlier test in this
        # module inserts one more message into the same shared mailbox.
        expect(page.get_by_text(re.compile(r"^\d+ selected$"))).to_be_visible(timeout=15_000)

        requests: list[Request] = []
        page.on(
            "request",
            lambda req: requests.append(req) if "/messages" in req.url or "/folders" in req.url
            else None,
        )

        page.get_by_role("toolbar", name="Bulk actions").get_by_role(
            "button", name="Mark as read", exact=True,
        ).click()

        # The write itself is the slow part (one statement over however
        # many rows match, server-side) -- wait for the bulk-action POST
        # to resolve, then a further quiet window for the event burst it
        # triggers to arrive and settle through the client's own
        # throttled flush before counting.
        expect(page.get_by_role("toolbar", name="Bulk actions")).to_have_count(
            0, timeout=30_000,
        )
        page.wait_for_timeout(4000)

        assert len(requests) <= 30, (
            f"marking the whole {_MAILBOX_SIZE}-message mailbox read issued "
            f"{len(requests)} requests -- expected a small, bounded number "
            f"regardless of how many rows the write actually touched: "
            f"{[r.url for r in requests]}"
        )
