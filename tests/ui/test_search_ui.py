"""
Search page: the folder picker's default and its persistence, semantic
mode hiding the field toggles, and the virtualization law itself -- a
search returning far more matches than fit on screen must still mount
only a bounded number of rows.

The fixture account here is seeded directly into the mirror, not through
a real Dovecot account -- these tests never touch IMAP, only what the
search endpoint reads out of accounts/folders/messages, so a bare row is
enough and avoids the cost of a real sync for a mailbox nothing here ever
reads mail *from*.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, Uuid, insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.ui.helpers import unique_email

_MATCH_COUNT = 1500
_MARKER = "scaletestmarker"


def _seed_scale_fixture(postgres_url: str, count: int) -> tuple[str, str]:
    """Bulk-insert a bare account, one folder, and `count` messages all
    matching _MARKER -- a single INSERT, the same shape
    tests/setup/large_mailbox.py uses, since delivering this many one at a
    time over LMTP would make the fixture itself the slow part of the
    test. Returns (account_id, folder_id) as strings."""

    async def _run() -> tuple[str, str]:
        engine = create_async_engine(postgres_url)
        account_id = uuid.uuid4()
        folder_id = uuid.uuid4()
        async with engine.begin() as conn:
            email = unique_email("search-scale")
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
            table = Table(
                "messages", MetaData(),
                Column("id", Uuid), Column("account_id", Uuid), Column("folder_id", Uuid),
                Column("imap_uid", BigInteger), Column("thread_id", Uuid),
                Column("message_id", Text),
                Column("subject", Text), Column("from_addr", Text),
                Column("received_at", DateTime(timezone=True)),
            )
            base_time = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [
                {
                    "id": uuid.uuid4(), "account_id": account_id, "folder_id": folder_id,
                    "imap_uid": i + 1, "thread_id": uuid.uuid4(),
                    "message_id": f"<{uuid.uuid4()}@search-scale.example.com>",
                    "subject": f"{_MARKER} item {i}",
                    "from_addr": f"sender{i % 20}@example.com",
                    "received_at": base_time + timedelta(minutes=i),
                }
                for i in range(count)
            ]
            await conn.execute(insert(table), rows)
        await engine.dispose()
        return str(account_id), str(folder_id)

    # Not a plain asyncio.run() -- pytest-playwright's sync API bridges to
    # its own asyncio loop by making one appear "running" on the main
    # thread for the duration of the browser/page fixtures (see
    # tests/ui/conftest.py's app_server docstring), and asyncio.run()
    # refuses to nest inside a loop that is already running.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture(scope="module")
def scale_fixture(postgres_url: str) -> tuple[str, str]:
    return _seed_scale_fixture(postgres_url, _MATCH_COUNT)


class TestSearchVirtualization:
    def test_thousands_of_matches_mount_a_bounded_number_of_rows(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        search_input = page.get_by_placeholder("Search messages…")
        expect(search_input).to_be_visible(timeout=15_000)
        search_input.fill(_MARKER)

        rows = page.locator('[data-testid="search-result-row"]')
        expect(rows.first).to_be_visible(timeout=15_000)

        # Give the list a moment to settle at its natural mounted count
        # (past the debounce and the first page's own load), then assert
        # it stays far below the match count -- proving virtualization
        # rather than merely that results appeared at all.
        page.wait_for_timeout(1500)
        mounted = rows.count()
        assert 0 < mounted < 150, (
            f"mounted {mounted} rows for {_MATCH_COUNT} matches -- not virtualized"
        )


class TestFolderPicker:
    def test_defaults_to_all_folders_and_a_change_persists_across_reload(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        trigger = page.get_by_role("button", name="All folders", exact=True)
        expect(trigger).to_be_visible(timeout=15_000)

        trigger.click()
        page.get_by_text("Deselect all", exact=True).click()
        expect(page.get_by_role("button", name="No folders", exact=True)).to_be_visible()

        page.reload()
        expect(page.get_by_role("button", name="No folders", exact=True)).to_be_visible(
            timeout=15_000
        )

        # Restore the default for any test running later in this module.
        page.get_by_role("button", name="No folders", exact=True).click()
        page.get_by_text("Select all", exact=True).click()
        expect(page.get_by_role("button", name="All folders", exact=True)).to_be_visible()


class TestSemanticMode:
    def test_toggling_semantic_hides_the_field_toggles(
        self, page: Page, app_server: str, scale_fixture: tuple[str, str],
    ) -> None:
        page.goto(f"{app_server}/search")
        subject_toggle = page.get_by_role("button", name="Subject", exact=True)
        expect(subject_toggle).to_be_visible(timeout=15_000)

        page.get_by_role("switch", name="Semantic search").click()

        # to_have_count(0) alone would pass before React ever re-renders --
        # poll for the positive case timing out is what actually proves
        # the toggles are gone rather than merely not-yet-checked.
        with pytest.raises(AssertionError):
            expect(subject_toggle).to_be_visible(timeout=5_000)

        page.get_by_role("switch", name="Semantic search").click()
        expect(subject_toggle).to_be_visible(timeout=5_000)
