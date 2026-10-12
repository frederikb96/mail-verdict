"""
Search's field-tier ordering and time window, plus a result row showing its
recipient -- all against a fixture seeded directly into the mirror, the
same approach test_search_ui.py already uses for anything not exercising
IMAP. One account: the search page's account scope defaults to whichever
account sorts first, not "every account", so a cross-account assertion
needs the unified view deliberately selected first -- out of scope here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import unique_email

_MARKER = "sortordertestmarker"


def _seed_ordering_and_window_fixture(postgres_url: str) -> tuple[str, str]:
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
    return _seed_ordering_and_window_fixture(postgres_url)


class TestOrdering:
    def test_results_are_ranked_by_field_tier_with_no_sort_control(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        older_id, _newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows).to_have_count(2, timeout=15_000)
        # The subject hit (older) outranks the body hit (newer).
        expect(rows.first).to_have_attribute("data-message-id", older_id)
        expect(page.get_by_role("button", name="Newest first", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Best match", exact=True)).to_have_count(0)


def _clear_window(page: Page, app_server: str) -> None:
    """Remove the persisted time window so a later test starts unfiltered."""
    page.evaluate("localStorage.removeItem('mailverdict:search-window')")
    page.reload()
    search_input = page.get_by_placeholder("Search messages…")
    expect(search_input).to_be_visible(timeout=15_000)
    search_input.fill(_MARKER)
    expect(page.locator('[data-testid="search-result-row"]')).to_have_count(2, timeout=15_000)


class TestTimeWindow:
    def test_a_custom_range_excludes_the_message_outside_it(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        _older_id, newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows).to_have_count(2, timeout=15_000)

        page.get_by_test_id("search-time-picker").click()
        page.get_by_role("menuitem", name="Custom…").click()
        page.get_by_test_id("search-time-custom-since").fill("2026-05-01")
        page.get_by_test_id("search-time-custom-until").fill("2026-07-01")
        page.get_by_test_id("search-time-custom-apply").click()

        expect(rows).to_have_count(1, timeout=10_000)
        expect(rows.first).to_have_attribute("data-message-id", newer_id)
        expect(page.get_by_test_id("search-time-chip")).to_contain_text("2026-05-01 – 2026-07-01")

        page.get_by_role("button", name="Clear time window").click()
        expect(rows).to_have_count(2, timeout=10_000)
        _clear_window(page, app_server)

    def test_dragging_the_slider_fires_one_search_not_one_per_tick(
        self, page: Page, app_server: str, sort_and_date_fixture: tuple[str, str],
    ) -> None:
        """A real pointer drag crosses many of the slider's stops; only the
        drag's own end may reach the server."""
        _older_id, newer_id = sort_and_date_fixture
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows).to_have_count(2, timeout=15_000)

        page.get_by_test_id("search-time-picker").click()
        page.get_by_role("menuitem", name="Slider…").click()
        slider_thumbs = page.get_by_role("slider")
        expect(slider_thumbs).to_have_count(2, timeout=10_000)
        # Opening selects the whole 52 weeks, which covers both messages.
        expect(rows).to_have_count(2, timeout=10_000)

        search_requests: list[str] = []

        def _track(request: object) -> None:
            url = request.url  # type: ignore[attr-defined]
            if "/api/search?" in url or "/api/embeddings/search?" in url:
                search_requests.append(url)

        page.on("request", _track)

        box = slider_thumbs.first.bounding_box()
        assert box is not None
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2

        page.mouse.move(start_x, start_y)
        page.mouse.down()
        # Many intermediate positions -- a quarter of the track or so,
        # past the older message's week and short of the newer one's.
        for i in range(1, 41):
            page.mouse.move(start_x + i * 4, start_y, steps=1)
        page.mouse.up()

        expect(rows).to_have_count(1, timeout=10_000)
        expect(rows.first).to_have_attribute("data-message-id", newer_id)
        assert len(search_requests) <= 1, (
            f"drag fired {len(search_requests)} searches, expected at most one "
            f"(on release): {search_requests!r}"
        )

        page.keyboard.press("Escape")
        _clear_window(page, app_server)


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
