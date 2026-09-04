"""
A message classified out of a folder while the reader is scrolled deep
into it must not move a single pixel of what they are currently looking
at -- the mail list holds its reading position across a removal from the
middle the same way it already does across mail arriving at the top.

Seeded directly into the mirror the same way tests/ui/test_mail_selection_
scale_ui.py does: enough rows that the reader's viewport sits well below
the row that gets removed, with real virtualization unmounting/remounting
rows along the way. The removal itself goes through postimap.actions.
move_message -- the same statement a real move (classification, a rule,
an explicit drag) issues -- rather than a raw UPDATE, so the NOTIFY ->
SSE -> cache-splice path this exercises is exactly the one production
code uses.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import text

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.postimap.actions import move_message
from tests.setup.large_mailbox import build_large_mailbox, seed_extra_folder
from tests.ui.helpers import mail_row, select_account

# Deep enough that the reader's anchor row sits many screens below the top,
# well past what a handful of scroll gestures would reach by accident, and
# with a comfortable band of rows between the removal target and the anchor.
_MAILBOX_SIZE = 400
# Newest-first display index of the row removed while the reader is deep in
# the list -- close enough to the top that it is unmounted by the time the
# anchor (below) is reached, far enough from index 0 that this is a genuine
# mid-list removal rather than the already-handled top-of-list case.
_REMOVED_DISPLAY_INDEX = 20
# Newest-first display index of the row whose on-screen position is the
# thing under test -- comfortably below the removal target, and comfortably
# inside where a deep scroll actually lands.
_ANCHOR_DISPLAY_INDEX = 150


@pytest.fixture(scope="module")
def anchor_mailbox(postgres_url: str) -> tuple[str, str, list[str]]:
    """One account, two folders: the folder under test holding
    `_MAILBOX_SIZE` messages, plus a bare second folder as the move
    target. Bridged through its own thread -- see big_mailbox in
    test_mail_selection_scale_ui.py for why."""

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
                await seed_extra_folder(session, account_id, imap_name="Elsewhere")
                await session.commit()
        finally:
            await connection.close()
        return str(account_id), str(folder_id), [str(m) for m in message_ids]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _seed()).result()


def _move_message_out(postgres_url: str, account_id: str, message_id: str) -> None:
    """Move one message out of its folder via the same statement
    production code issues, into the bare second folder seeded above."""

    async def _move() -> None:
        connection = DatabaseConnection(
            DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
        )
        await connection.init()
        try:
            async with connection.session() as session:
                target_folder_id = (
                    await session.execute(
                        text(
                            "SELECT id FROM folders WHERE account_id = :account_id "
                            "AND imap_name = 'Elsewhere'"
                        ),
                        {"account_id": uuid.UUID(account_id)},
                    )
                ).scalar_one()
                moved = await move_message(
                    session, uuid.UUID(message_id), target_folder_id,
                )
                assert moved == 1, f"expected to move exactly one row, moved {moved}"
                await session.commit()
        finally:
            await connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _move()).result()


_CAPTURE_FOLDER_ID_CHANGES_SCRIPT = """
window.__folderIdChangeIds = [];
const RealEventSource = window.EventSource;
window.EventSource = class extends RealEventSource {
  constructor(...args) {
    super(...args);
    this.addEventListener('mail.updated', (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.changed && data.changed.includes('folder_id')) {
          window.__folderIdChangeIds.push(data.id);
        }
      } catch (err) { /* ignore */ }
    });
  }
};
"""


class TestMailListScrollAnchorUi:
    def test_removing_a_message_above_the_reader_does_not_move_their_row(
        self,
        page: Page,
        app_server: str,
        postgres_url: str,
        anchor_mailbox: tuple[str, str, list[str]],
    ) -> None:
        account_id, _folder_id, message_ids = anchor_mailbox
        # Newest-first: display index 0 is the last-inserted message.
        removed_id = message_ids[-1 - _REMOVED_DISPLAY_INDEX]
        anchor_id = message_ids[-1 - _ANCHOR_DISPLAY_INDEX]

        # removed_row already sits outside the viewport once scrolled deep,
        # so its own absence can't tell "removed" apart from "just
        # unmounted by virtualization" -- instrument the SSE stream itself
        # instead of guessing a wait long enough for the round trip.
        page.add_init_script(_CAPTURE_FOLDER_ID_CHANGES_SCRIPT)
        page.goto(app_server)
        select_account(page, {"name": f"large-mailbox-{account_id}"})
        # Two folders exist on this account (the second is the move target
        # seeded above) -- the default folder selection is not guaranteed
        # to land on the one actually holding the mailbox under test.
        page.get_by_role("button", name="INBOX", exact=True).click()

        top_row = mail_row(page, message_ids[-1])
        expect(top_row).to_be_visible(timeout=15_000)

        anchor_row = mail_row(page, anchor_id)
        list_area = page.locator('[data-testid="mail-row"]').first
        list_area.hover()
        for _ in range(60):
            if anchor_row.is_visible():
                break
            page.mouse.wheel(0, 600)
        expect(anchor_row).to_be_visible(timeout=15_000)
        # Let scroll-triggered pagination settle before taking the "before"
        # measurement -- fetchNextPage lands a beat behind the scroll itself.
        page.wait_for_timeout(1000)

        removed_row = mail_row(page, removed_id)
        expect(removed_row).not_to_be_visible()  # scrolled well past it

        before_box = anchor_row.bounding_box()
        assert before_box is not None, "anchor row has no bounding box before the removal"

        _move_message_out(postgres_url, account_id, removed_id)

        # Proves the SSE event itself arrived -- the definitive signal,
        # unlike removed_row's own visibility (see the comment above).
        page.wait_for_function(
            "(id) => window.__folderIdChangeIds && window.__folderIdChangeIds.includes(id)",
            arg=removed_id,
            timeout=10_000,
        )
        # The client buffers mail.updated for FLUSH_INTERVAL_MS (500ms)
        # before splicing the cache and re-rendering -- give that, the
        # render, and the correction's own layout effect room to land.
        page.wait_for_timeout(1500)

        after_box = anchor_row.bounding_box()
        assert after_box is not None, "anchor row has no bounding box after the removal"

        delta = after_box["y"] - before_box["y"]
        assert delta == pytest.approx(0, abs=1), (
            f"anchor row moved {delta:.1f}px when a message above it and outside "
            f"the viewport was removed from the folder (before y={before_box['y']:.1f}, "
            f"after y={after_box['y']:.1f})"
        )
