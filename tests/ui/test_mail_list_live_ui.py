"""
Mail arriving or changing while a folder is open must reach the list and
every count beside it at the same moment, whatever the reader is doing --
scrolled deep into a folder, resting at the top of it, looking at a
conversation whose unread message is not its newest, reading a unified
view, or reconnecting after the live stream dropped.

New mail is inserted straight into the mirror, the same way
test_mail_selection_scale_ui.py delivers one more message: the insert
fires PostIMAP's own postimap_events NOTIFY, so the NOTIFY -> SSE ->
client path exercised here is the one a real sync takes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect
from sqlalchemy import text

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from tests.setup.large_mailbox import build_large_mailbox
from tests.ui.helpers import (
    add_folder_to_unified_view,
    folder,
    folder_button,
    mail_row,
    select_account,
)

# Six pages of fifty -- comfortably past any page-count threshold a list
# refresh could be gated on, so a deep reader genuinely has many pages
# loaded when mail arrives.
_MAILBOX_SIZE = 300

# Each insert takes a fresh UID above the seeded range, so repeated inserts
# across the module never collide on UNIQUE(folder_id, imap_uid).
_next_uid = itertools.count(_MAILBOX_SIZE + 1_000)

_INSERT_MESSAGE = text(
    "INSERT INTO messages (id, account_id, folder_id, imap_uid, thread_id, message_id, "
    "subject, from_addr, received_at, is_seen) VALUES (:id, :account_id, :folder_id, "
    ":imap_uid, :thread_id, :message_id, :subject, :from_addr, :received_at, :is_seen)"
)

# The scroll container a row sits in, found by walking up from the row
# itself rather than by class name, plus where the row sits inside it.
_ROW_GEOMETRY_SCRIPT = """(id) => {
  const row = document.querySelector(`[data-testid="mail-row"][data-mail-id="${id}"]`);
  if (!row) return null;
  let el = row.parentElement;
  while (el && !['auto', 'scroll'].includes(getComputedStyle(el).overflowY)) {
    el = el.parentElement;
  }
  if (!el) return null;
  const r = row.getBoundingClientRect();
  const s = el.getBoundingClientRect();
  return {
    rowTop: r.top, rowBottom: r.bottom, viewTop: s.top, viewBottom: s.bottom,
    scrollTop: el.scrollTop,
  };
}"""

_CAPTURE_EVENT_SOURCES_SCRIPT = """
window.__mailNewIds = [];
window.__eventSources = [];
const RealEventSource = window.EventSource;
window.EventSource = class extends RealEventSource {
  constructor(...args) {
    super(...args);
    window.__eventSources.push(this);
    this.addEventListener('mail.new', (e) => {
      try { window.__mailNewIds.push(JSON.parse(e.data).id); } catch (err) { /* ignore */ }
    });
  }
};
"""


@pytest.fixture(scope="module")
def live_mailbox(postgres_url: str) -> tuple[str, str, list[str]]:
    """One account, one INBOX holding `_MAILBOX_SIZE` messages, one in five
    unread."""

    async def _seed() -> tuple[str, str, list[str]]:
        connection = DatabaseConnection(
            DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
        )
        await connection.init()
        try:
            async with connection.session() as session:
                account_id, folder_id, message_ids = await build_large_mailbox(
                    session,
                    _MAILBOX_SIZE,
                )
                await session.commit()
        finally:
            await connection.close()
        return str(account_id), str(folder_id), [str(m) for m in message_ids]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _seed()).result()


def _insert_messages(
    postgres_url: str,
    account_id: str,
    folder_id: str,
    rows: list[dict[str, object]],
) -> None:
    """Insert rows into the mirror in one transaction. Each row names its
    own id, thread_id, subject, received_at and is_seen; everything else
    is filled in here."""

    async def _insert() -> None:
        connection = DatabaseConnection(
            DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
        )
        await connection.init()
        try:
            async with connection.session() as session:
                for row in rows:
                    await session.execute(
                        _INSERT_MESSAGE,
                        {
                            "account_id": uuid.UUID(account_id),
                            "folder_id": uuid.UUID(folder_id),
                            "imap_uid": next(_next_uid),
                            "message_id": f"<{row['id']}@live-list.example.com>",
                            "from_addr": "live@example.com",
                            **row,
                        },
                    )
                await session.commit()
        finally:
            await connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _insert()).result()


def _new_message(
    *, is_seen: bool = False, age_s: int = 0, thread_id: uuid.UUID | None = None
) -> dict[str, object]:
    message_id = uuid.uuid4()
    return {
        "id": message_id,
        "thread_id": thread_id or uuid.uuid4(),
        "subject": f"Live arrival {message_id.hex[:8]}",
        "received_at": datetime.now(UTC) - timedelta(seconds=age_s),
        "is_seen": is_seen,
    }


_BADGE_COUNT_SCRIPT = """(el) => {
  const numbers = [...el.querySelectorAll('*')]
    .filter((node) => node.children.length === 0)
    .map((node) => (node.textContent || '').trim())
    .filter((text) => /^[0-9]+$/.test(text));
  return numbers.length ? Number(numbers[numbers.length - 1]) : 0;
}"""


def _badge_count(item: Locator) -> int:
    """The number a sidebar folder item's badge carries, 0 when it has none.
    Read from the badge element's own text, whether or not it is showing:
    the badge hides while its row is hovered, which is exactly where the
    pointer rests after clicking the folder, and a folder's own name can end
    in digits that run straight into the badge's in the item's text."""
    return int(item.evaluate(_BADGE_COUNT_SCRIPT))


def _wait_for_badge(item: Locator, expected: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = _badge_count(item)
    while time.monotonic() < deadline:
        last = _badge_count(item)
        if last == expected:
            return
        time.sleep(0.25)
    raise AssertionError(f"sidebar count stayed {last}, expected {expected}")


def _settled_folder_badge(
    item: Locator, api_client: httpx.Client, account_id: str, folder_id: str
) -> int:
    """The folder's badge once it shows what the server reports -- read
    any earlier and it is still the empty first paint."""
    resp = api_client.get(f"/api/accounts/{account_id}/folders")
    assert resp.status_code == 200, resp.text
    expected = next(f["unread_count"] for f in resp.json() if f["id"] == folder_id)
    _wait_for_badge(item, expected)
    return expected


def _settled_unified_badge(item: Locator, api_client: httpx.Client, unified_name: str) -> int:
    resp = api_client.get("/api/unified/folders")
    assert resp.status_code == 200, resp.text
    expected = next(g["unread_count"] for g in resp.json() if g["unified_name"] == unified_name)
    _wait_for_badge(item, expected)
    return expected


def _open_inbox(page: Page, app_server: str, account_id: str, folder_id: str) -> None:
    page.goto(app_server)
    account_name = f"large-mailbox-{account_id}"
    # This module's account is usually the only one, so it is selected on its
    # own; switching through the dropdown only when it is not.
    selected = page.locator('[data-slot="sidebar-header"]').get_by_role(
        "button", name=account_name, exact=True
    )
    try:
        expect(selected).to_be_visible(timeout=15_000)
    except AssertionError:
        select_account(page, {"name": account_name})
    folder_button(page, folder_id).click()
    expect(page.locator('[data-testid="mail-row"]').first).to_be_visible(timeout=15_000)


# The list's scroll offset, read through whichever row is mounted -- a row
# picked by id is unmounted once the list is scrolled away from it.
_LIST_SCROLL_TOP_SCRIPT = """() => {
  const row = document.querySelector('[data-testid="mail-row"]');
  let el = row && row.parentElement;
  while (el && !['auto', 'scroll'].includes(getComputedStyle(el).overflowY)) {
    el = el.parentElement;
  }
  return el ? el.scrollTop : null;
}"""


def _scroll_list_to_top(page: Page) -> None:
    page.locator('[data-testid="mail-row"]').first.hover()
    for _ in range(80):
        if page.evaluate(_LIST_SCROLL_TOP_SCRIPT) == 0:
            return
        page.mouse.wheel(0, -4000)
        page.wait_for_timeout(100)
    raise AssertionError(
        f"the list never reached the top: scrollTop {page.evaluate(_LIST_SCROLL_TOP_SCRIPT)}"
    )


def _expect_unread(row: Locator) -> None:
    """A row whose own toggle offers "Mark as read" is shown as unread --
    the same predicate drives its dot, weight and background."""
    expect(row.get_by_role("button", name="Mark as read", exact=True)).to_have_count(
        1,
        timeout=15_000,
    )


def _expect_read(row: Locator) -> None:
    expect(row.get_by_role("button", name="Mark as unread", exact=True)).to_have_count(
        1,
        timeout=15_000,
    )


class TestMailListLiveUi:
    def test_new_mail_reaches_a_deeply_scrolled_list_as_well_as_its_count(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, folder_id, message_ids = live_mailbox
        _open_inbox(page, app_server, account_id, folder_id)
        inbox = folder(page, folder_id)
        unread_before = _settled_folder_badge(inbox, api_client, account_id, folder_id)

        # Every page loaded: the oldest seeded message is the list's last row.
        oldest = mail_row(page, message_ids[0])
        page.locator('[data-testid="mail-row"]').first.hover()
        for _ in range(120):
            if oldest.count() > 0:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(150)
        expect(oldest).to_have_count(1, timeout=15_000)
        page.wait_for_timeout(1000)

        arrival = _new_message()
        _insert_messages(postgres_url, account_id, folder_id, [arrival])

        # The count reaching the sidebar proves the live event arrived and
        # was acted on; the list must have been refreshed by that same act.
        _wait_for_badge(inbox, unread_before + 1)
        page.wait_for_timeout(1500)

        _scroll_list_to_top(page)
        arrived_row = mail_row(page, str(arrival["id"]))
        expect(arrived_row).to_have_count(1, timeout=5_000)
        _expect_unread(arrived_row)

    def test_a_conversation_whose_unread_message_is_not_its_newest_reads_as_unread(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        """Grouped by conversation, a row stands for its newest message --
        but the folder's unread count counts every message. An older unread
        reply behind a read newest one must show the row as unread, and
        opening the conversation must clear both the row and the count."""
        account_id, folder_id, _message_ids = live_mailbox
        _open_inbox(page, app_server, account_id, folder_id)
        inbox = folder(page, folder_id)
        unread_before = _settled_folder_badge(inbox, api_client, account_id, folder_id)

        thread_id = uuid.uuid4()
        older_unread = _new_message(is_seen=False, age_s=60, thread_id=thread_id)
        newer_read = _new_message(is_seen=True, age_s=30, thread_id=thread_id)
        _insert_messages(postgres_url, account_id, folder_id, [older_unread, newer_read])
        _wait_for_badge(inbox, unread_before + 1)

        conversation = mail_row(page, str(newer_read["id"]))
        expect(conversation).to_have_count(1, timeout=15_000)
        _expect_unread(conversation)

        conversation.get_by_text(str(newer_read["subject"]), exact=True).click()
        _wait_for_badge(inbox, unread_before)
        _expect_read(conversation)

    def test_a_list_resting_at_the_top_shows_new_mail_without_scrolling(
        self,
        page: Page,
        app_server: str,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, folder_id, _message_ids = live_mailbox
        _open_inbox(page, app_server, account_id, folder_id)
        top_before = page.locator('[data-testid="mail-row"]').first.get_attribute("data-mail-id")
        assert top_before is not None
        before = page.evaluate(_ROW_GEOMETRY_SCRIPT, top_before)
        assert before is not None and before["scrollTop"] == 0, before

        arrival = _new_message()
        _insert_messages(postgres_url, account_id, folder_id, [arrival])
        arrived_row = mail_row(page, str(arrival["id"]))
        expect(arrived_row).to_have_count(1, timeout=15_000)
        page.wait_for_timeout(1000)

        after = page.evaluate(_ROW_GEOMETRY_SCRIPT, str(arrival["id"]))
        assert after is not None
        assert after["scrollTop"] <= 1, (
            f"the list scrolled to {after['scrollTop']}px to keep the old top row in "
            f"place, leaving the new message above what the reader can see"
        )
        assert (
            after["viewTop"] - 1 <= after["rowTop"]
            and after["rowBottom"] <= after["viewBottom"] + 1
        ), f"new message is outside the list viewport: {after}"

    def test_a_reader_scrolled_down_keeps_their_place_when_mail_arrives(
        self,
        page: Page,
        app_server: str,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, folder_id, message_ids = live_mailbox
        page.add_init_script(_CAPTURE_EVENT_SOURCES_SCRIPT)
        _open_inbox(page, app_server, account_id, folder_id)

        anchor_row = mail_row(page, message_ids[-40])
        page.locator('[data-testid="mail-row"]').first.hover()
        for _ in range(40):
            geometry = page.evaluate(_ROW_GEOMETRY_SCRIPT, message_ids[-40])
            if (
                geometry is not None
                and geometry["viewTop"] + 100 <= geometry["rowTop"] <= geometry["viewBottom"] - 150
            ):
                break
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(100)
        expect(anchor_row).to_be_visible()
        page.wait_for_timeout(1000)
        before_box = anchor_row.bounding_box()
        assert before_box is not None

        arrival = _new_message()
        _insert_messages(postgres_url, account_id, folder_id, [arrival])
        page.wait_for_function(
            "(id) => window.__mailNewIds.includes(id)",
            arg=str(arrival["id"]),
            timeout=15_000,
        )
        page.wait_for_timeout(1500)

        after_box = anchor_row.bounding_box()
        assert after_box is not None
        delta = after_box["y"] - before_box["y"]
        assert delta == pytest.approx(0, abs=1), (
            f"the row the reader was looking at moved {delta:.1f}px when mail arrived above it"
        )

    def test_the_unified_view_receives_new_mail_live(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, folder_id, _message_ids = live_mailbox
        unified_name = f"Live unified {uuid.uuid4().hex[:8]}"
        add_folder_to_unified_view(api_client, folder_id, unified_name)

        page.goto(app_server)
        # The account switcher only opens once the page has hydrated; the
        # auto-selected account appearing in it is the sign that it has.
        trigger = page.locator('[data-slot="sidebar-header"]').get_by_role(
            "button", name=f"large-mailbox-{account_id}", exact=True
        )
        expect(trigger).to_be_visible(timeout=15_000)
        unified_entry = page.locator('[data-slot="dropdown-menu-item"]').get_by_text(
            "Unified View", exact=True
        )
        for _ in range(3):
            trigger.click()
            try:
                expect(unified_entry).to_be_visible(timeout=5_000)
                break
            except AssertionError:
                page.keyboard.press("Escape")
        unified_entry.click()
        unified_item = page.locator('[data-testid="folder"]').filter(has_text=unified_name)
        unified_item.get_by_role("button").first.click()
        expect(page.locator('[data-testid="mail-row"]').first).to_be_visible(timeout=15_000)
        unread_before = _settled_unified_badge(unified_item, api_client, unified_name)

        arrival = _new_message()
        _insert_messages(postgres_url, account_id, folder_id, [arrival])
        _wait_for_badge(unified_item, unread_before + 1)

        arrived_row = mail_row(page, str(arrival["id"]))
        expect(arrived_row).to_have_count(1, timeout=10_000)
        page.wait_for_timeout(1000)
        after = page.evaluate(_ROW_GEOMETRY_SCRIPT, str(arrival["id"]))
        assert after is not None
        assert after["scrollTop"] <= 1 and after["viewTop"] - 1 <= after["rowTop"], (
            f"new message is outside the unified list viewport: {after}"
        )

    def test_mail_that_arrived_while_the_live_stream_was_down_appears_after_reconnect(
        self,
        page: Page,
        app_server: str,
        postgres_url: str,
        live_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, folder_id, _message_ids = live_mailbox
        page.add_init_script(_CAPTURE_EVENT_SOURCES_SCRIPT)
        _open_inbox(page, app_server, account_id, folder_id)
        page.wait_for_function("() => window.__eventSources.some((s) => s.readyState === 1)")

        # Refuse every reconnect attempt, then drop the live connection --
        # the client's own error path closes it and starts retrying.
        page.route("**/api/events**", lambda route: route.abort())
        page.evaluate("() => window.__eventSources.at(-1).dispatchEvent(new Event('error'))")
        page.wait_for_timeout(500)

        arrival = _new_message()
        _insert_messages(postgres_url, account_id, folder_id, [arrival])
        page.wait_for_timeout(2000)
        arrived_row = mail_row(page, str(arrival["id"]))
        assert arrived_row.count() == 0, "the message reached the list with the stream down"

        page.unroute("**/api/events**")
        expect(arrived_row).to_have_count(1, timeout=45_000)
