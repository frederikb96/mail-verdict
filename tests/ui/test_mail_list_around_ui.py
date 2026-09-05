"""
Opening a message from search when it sits far down its folder -- the
mail list has to answer a question it never had to before: a fetch
window centred on a message rather than the newest edge (GET
/accounts/:id/messages?around=...), and revealing that message in the
upper third once it loads, through the same scroll writer as every
other positioning in mail-list.tsx.

Seeded directly into the mirror, the same shape test_search_ui.py's own
scale fixture uses -- these tests never touch IMAP, only what the
messages/search endpoints read out of accounts/folders/messages.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, Uuid, insert
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import select_account, unique_email

_FILLER_SUBJECT = "around test filler"
_TARGET_SUBJECT = "aroundtestuniquetarget"
_FOLDER_SIZE = 500


def _seed_deep_target(postgres_url: str) -> tuple[str, str, str]:
    """One account, one folder, `_FOLDER_SIZE` ordinary messages ascending
    by received_at, plus one uniquely-subjected message in the middle of
    that span -- newest-first, comfortably below the newest page and with
    plenty of older messages still beneath it too. Returns (account_id,
    account_email, target_message_id)."""

    async def _run() -> tuple[str, str, str]:
        engine = create_async_engine(postgres_url)
        account_id = uuid.uuid4()
        folder_id = uuid.uuid4()
        target_id = uuid.uuid4()
        async with engine.begin() as conn:
            email = unique_email("around-scale")
            await conn.execute(
                sa_text(
                    "INSERT INTO accounts "
                    "(id, name, imap_host, imap_port, imap_user, imap_password) "
                    "VALUES (:id, :name, 'imap.example.com', 993, :email, "
                    "'\\x00' || convert_to('pw', 'UTF8'))"
                ),
                {"id": account_id, "name": email, "email": email},
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO folders (id, account_id, imap_name) "
                    "VALUES (:id, :account_id, 'INBOX')"
                ),
                {"id": folder_id, "account_id": account_id},
            )
            table = Table(
                "messages", MetaData(),
                Column("id", Uuid), Column("account_id", Uuid), Column("folder_id", Uuid),
                Column("imap_uid", BigInteger), Column("thread_id", Uuid),
                Column("message_id", Text),
                Column("subject", Text), Column("from_addr", Text),
                Column("received_at", DateTime(timezone=True)),
            )
            # The target sits in the middle of the folder's own history --
            # plenty of both older and newer filler around it, so there is
            # room to scroll in either direction. Placed at the very oldest
            # or newest extreme instead, the browser has nothing left to
            # scroll past it into and clamps, which reads as a broken
            # reveal when it is actually just nowhere to put it.
            base_time = datetime(2020, 1, 1, tzinfo=UTC)
            target_time = base_time + timedelta(minutes=_FOLDER_SIZE // 2)
            rows = [
                {
                    "id": target_id, "account_id": account_id, "folder_id": folder_id,
                    "imap_uid": 1, "thread_id": target_id,
                    "message_id": f"<{target_id}@around-scale.example.com>",
                    "subject": _TARGET_SUBJECT,
                    "from_addr": "sender0@example.com",
                    "received_at": target_time,
                },
                *(
                    {
                        "id": uuid.uuid4(), "account_id": account_id, "folder_id": folder_id,
                        "imap_uid": i + 2, "thread_id": uuid.uuid4(),
                        "message_id": f"<{uuid.uuid4()}@around-scale.example.com>",
                        "subject": f"{_FILLER_SUBJECT} {i}",
                        "from_addr": f"sender{i % 20}@example.com",
                        "received_at": base_time + timedelta(minutes=i),
                    }
                    for i in range(_FOLDER_SIZE)
                    if i != _FOLDER_SIZE // 2
                ),
            ]
            await conn.execute(insert(table), rows)
        await engine.dispose()
        return str(account_id), email, str(target_id)

    # Not a plain asyncio.run() -- pytest-playwright's sync API bridges to
    # its own asyncio loop by making one appear "running" on the main
    # thread for the duration of the browser/page fixtures (see
    # tests/ui/conftest.py's app_server docstring), and asyncio.run()
    # refuses to nest inside a loop that is already running.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture(scope="module")
def deep_target_fixture(postgres_url: str) -> tuple[str, str, str]:
    return _seed_deep_target(postgres_url)


class TestOpenSearchResultLandsDeepInTheFolder:
    def test_the_opened_message_sits_in_the_upper_third_with_no_visible_jump(
        self, page: Page, app_server: str, deep_target_fixture: tuple[str, str, str],
    ) -> None:
        _account_id, _account_email, target_id = deep_target_fixture

        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_TARGET_SUBJECT)

        result_row = page.locator('[data-testid="search-result-row"]')
        expect(result_row).to_have_count(1, timeout=15_000)
        result_row.click()

        expect(page).to_have_url(f"{app_server}/")
        target_row = page.locator(f'[data-testid="mail-row"][data-mail-id="{target_id}"]')
        expect(target_row).to_be_visible(timeout=15_000)

        # Give the reveal's own scrollToIndex, and anything virtua measures
        # while it settles, a moment before reading the final position --
        # the acceptance bound is on where it ends up, not on there being
        # no motion at all while rows around it mount and get measured.
        page.wait_for_timeout(500)

        row_box = target_row.bounding_box()
        assert row_box is not None
        # The row's own nearest scrolling ancestor is the actual viewport
        # the reveal positions against -- not the page body, and not
        # assumed from markup structure that could change.
        container_top = target_row.evaluate(
            """(el) => {
                let node = el.parentElement;
                while (node && node !== document.body) {
                    const style = getComputedStyle(node);
                    if (style.overflowY === "auto" || style.overflowY === "scroll") {
                        return node.getBoundingClientRect().top;
                    }
                    node = node.parentElement;
                }
                return 0;
            }"""
        )
        viewport_size = page.viewport_size
        assert viewport_size is not None
        viewport_height = viewport_size["height"]

        offset_from_top = row_box["y"] - container_top
        target_offset = viewport_height / 3
        # Within about a tenth of a third of the viewport height, per the
        # row's own acceptance bound.
        assert abs(offset_from_top - target_offset) < viewport_height / 30, (
            f"opened message sits {offset_from_top:.0f}px from the list's own top, "
            f"expected close to viewport_height/3 = {target_offset:.0f}px"
        )

        # Going back returns to the search results unchanged.
        page.go_back()
        expect(page).to_have_url(f"{app_server}/search")
        expect(page.locator('[data-testid="search-result-row"]')).to_have_count(1)

    def test_reopening_from_the_folder_view_leaves_the_edge_view_unaffected(
        self, page: Page, app_server: str, deep_target_fixture: tuple[str, str, str],
    ) -> None:
        """An ordinary folder open (no `around`) is untouched by any of
        this -- it still starts at the newest edge."""
        _account_id, account_email, target_id = deep_target_fixture

        page.goto(app_server)
        select_account(page, {"name": account_email})
        newest_row = page.locator('[data-testid="mail-row"]').first
        expect(newest_row).to_be_visible(timeout=15_000)
        # The target (the single oldest message) is not part of the first,
        # newest-first page at all.
        expect(
            page.locator(f'[data-testid="mail-row"][data-mail-id="{target_id}"]')
        ).not_to_be_visible()
