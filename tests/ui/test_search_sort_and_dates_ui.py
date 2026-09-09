"""
Search's sort toggle and date-range filter, plus a result row showing its
recipient -- all against a fixture seeded directly into the mirror, the
same approach test_search_ui.py already uses for anything not exercising
IMAP. One account: the search page's account scope defaults to whichever
account sorts first, not "every account", so a cross-account assertion
needs the unified view deliberately selected first -- out of scope here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import uuid
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import unique_email

_MARKER = "sortordertestmarker"


def _seed_sort_and_date_fixture(postgres_url: str) -> tuple[str, str]:
    """One account, one folder: an older message whose subject carries the
    marker (tier 0, ranks first under relevance) and a newer one that
    only carries it in the body (tier 3), the newer one also carrying a
    recipient so the row's own To-line has something to show."""

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(postgres_url)
        account_id = uuid.uuid4()
        folder_id = uuid.uuid4()
        async with engine.begin() as conn:
            email = unique_email("sortdate")
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
                {"id": folder_id, "account_id": account_id},
            )

            older_id = uuid.uuid4()
            newer_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
                    "subject, from_addr, to_addrs, body_text, received_at) "
                    "VALUES (:id, :account_id, :folder_id, 1, :thread_id, :msg_id, "
                    ":subject, :from_addr, NULL, :body_text, :received_at)"
                ),
                {
                    "id": older_id, "account_id": account_id, "folder_id": folder_id,
                    "thread_id": uuid.uuid4(), "msg_id": f"<{older_id}@example.com>",
                    "subject": f"{_MARKER} report", "from_addr": "sender@example.com",
                    "body_text": "nothing relevant",
                    "received_at": datetime(2026, 1, 1, tzinfo=UTC),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(id, account_id, folder_id, imap_uid, thread_id, message_id, "
                    "subject, from_addr, to_addrs, body_text, received_at) "
                    "VALUES (:id, :account_id, :folder_id, 2, :thread_id, :msg_id, "
                    "'unrelated subject', :from_addr, :to_addrs, :body_text, :received_at)"
                ),
                {
                    "id": newer_id, "account_id": account_id, "folder_id": folder_id,
                    "thread_id": uuid.uuid4(), "msg_id": f"<{newer_id}@example.com>",
                    "from_addr": "sender@example.com",
                    "to_addrs": '["Alice Recipient <alice@example.com>"]',
                    "body_text": f"the {_MARKER} appears here",
                    "received_at": datetime(2026, 6, 1, tzinfo=UTC),
                },
            )
        await engine.dispose()
        return str(older_id), str(newer_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture(scope="module")
def sort_and_date_fixture(postgres_url: str) -> tuple[str, str]:
    return _seed_sort_and_date_fixture(postgres_url)


class TestSortToggle:
    def test_newest_first_reorders_a_tier_3_hit_ahead_of_a_tier_0_one(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        older_id, newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows).to_have_count(2, timeout=15_000)
        # Default is relevance: the subject hit (older) ranks first.
        expect(rows.first).to_have_attribute("data-message-id", older_id)

        page.get_by_role("button", name="Newest first", exact=True).click()
        expect(rows.first).to_have_attribute("data-message-id", newer_id, timeout=10_000)

        page.get_by_role("button", name="Best match", exact=True).click()
        expect(rows.first).to_have_attribute("data-message-id", older_id, timeout=10_000)

    def test_the_chosen_sort_mode_persists_across_reload(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        expect(page.get_by_role("button", name="Best match", exact=True)).to_be_visible(
            timeout=15_000
        )
        page.get_by_role("button", name="Newest first", exact=True).click()

        page.reload()
        newest_button = page.get_by_role("button", name="Newest first", exact=True)
        expect(newest_button).to_be_visible(timeout=15_000)
        # A pressed toggle carries the primary-tinted class this app's
        # other field/strictness toggles already use -- checking for it
        # is what tells "selected" apart from merely "present".
        expect(newest_button).to_have_class(re.compile(r"border-primary"))

        # Restore the default for any test running later in this module.
        page.get_by_role("button", name="Best match", exact=True).click()


class TestDateRangeFilter:
    def test_narrowing_the_range_excludes_the_message_outside_it(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        _older_id, newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows).to_have_count(2, timeout=15_000)

        page.get_by_role("button", name="All time", exact=True).click()
        # get_by_role, not a `[role="slider"]` CSS selector -- the thumb's
        # own input[type=range] carries the role implicitly rather than
        # as a literal HTML attribute, which only the former resolves.
        slider_thumbs = page.get_by_role("slider")
        expect(slider_thumbs).to_have_count(2, timeout=10_000)

        # Drag the lower handle most of the way to the upper end -- past
        # the older message's own date, short of the newer one's --
        # keyboard stepping rather than a pointer drag, since a slider's
        # exact pixel-to-value mapping isn't this test's own concern.
        slider_thumbs.first.focus()
        for _ in range(60):
            page.keyboard.press("ArrowRight")

        expect(rows).to_have_count(1, timeout=10_000)
        expect(rows.first).to_have_attribute("data-message-id", newer_id)

        # Restore the default for any later test in this module -- not
        # through the popover again (each drag re-renders the slider's own
        # positions, which raced Playwright's click-stability check when
        # driven the same way immediately afterward); clearing the
        # persisted atom directly is what the reload-driven persistence
        # tests elsewhere in this app's suite already do for their own.
        page.evaluate("localStorage.removeItem('mailverdict:search-date-range')")
        page.reload()
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)
        expect(rows).to_have_count(2, timeout=15_000)


class TestRecipientDisplay:
    def test_a_result_shows_its_recipient(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        _older_id, newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        row = page.locator(f'[data-testid="search-result-row"][data-message-id="{newer_id}"]')
        expect(row).to_be_visible(timeout=15_000)
        # .first: get_by_text also matches the row's own outer button,
        # whose full text content concatenates every descendant's -- the
        # actual To-line span is what .first resolves to (DOM order).
        expect(row.get_by_text("Alice Recipient", exact=False).first).to_be_visible()
