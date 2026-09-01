"""
UI-layer helpers: locators against the data-testid attributes carried by
DragMail (mail rows) and DroppableFolder (sidebar folders), a raw-pointer
drag sequence, and the two account/folder polling helpers re-implemented
against a real httpx.Client rather than Starlette's TestClient.

unique_email, wait_for, wait_for_async, and wait_for_mailpit_message carry
no client-type dependency, so tests/e2e/helpers.py's versions are reused
directly rather than duplicated here.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from playwright.sync_api import Locator, Page

from tests.e2e.helpers import (  # noqa: F401 -- re-exported for tests/ui/ callers
    unique_email,
    wait_for,
    wait_for_mailpit_message,
)

# Raw-pointer sequence, not locator.drag_to(): dnd-kit only activates its
# PointerSensor on a real mousedown/mousemove/mouseup sequence with an
# activation-distance move first, and Playwright's own drag_to() additionally
# fails here on strict-mode name collisions between a folder button and a
# row's own "Move to trash" button of the same name.
_DRAG_STEPS = 20
_ACTIVATION_PX = 12


def mail_row(page: Page, mail_id: str) -> Locator:
    """The draggable wrapper around one mail row, by its message id."""
    return page.locator(f'[data-testid="mail-row"][data-mail-id="{mail_id}"]')


def folder(page: Page, folder_id: str) -> Locator:
    """The droppable sidebar folder item, by its folder id."""
    return page.locator(f'[data-testid="folder"][data-folder-id="{folder_id}"]')


def drag_row_to_folder(page: Page, row: Locator, target: Locator) -> None:
    """Drag a mail row onto a folder with a real pointer sequence.

    dnd-kit's rectIntersection collision detection compares the dragged
    row's whole rectangle against every droppable, not the pointer position
    alone -- ending the move over the target's centre is what makes the
    target, rather than a neighbour, the one dnd-kit reports as `over`.
    """
    row_box = row.bounding_box()
    target_box = target.bounding_box()
    assert row_box is not None, "drag source has no bounding box -- not visible?"
    assert target_box is not None, "drop target has no bounding box -- not visible?"

    start_x = row_box["x"] + row_box["width"] / 2
    start_y = row_box["y"] + row_box["height"] / 2
    end_x = target_box["x"] + target_box["width"] / 2
    end_y = target_box["y"] + target_box["height"] / 2

    page.mouse.move(start_x, start_y)
    page.mouse.down()
    # A move smaller than dnd-kit's activation distance is swallowed as a
    # click rather than starting a drag.
    page.mouse.move(start_x, start_y - _ACTIVATION_PX - 1)
    for step in range(1, _DRAG_STEPS + 1):
        fraction = step / _DRAG_STEPS
        page.mouse.move(
            start_x + (end_x - start_x) * fraction,
            start_y - _ACTIVATION_PX - 1 + (end_y - (start_y - _ACTIVATION_PX - 1)) * fraction,
        )
    page.mouse.up()


def wait_for_account_active(
    client: httpx.Client, account_id: str, timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll an account until PostIMAP reports it `active`.

    Fails immediately (not after the full timeout) if PostIMAP reports
    `error` instead -- that state will never self-resolve into `active`.
    """
    deadline = time.monotonic() + timeout_s
    last_state = "unknown"
    while time.monotonic() < deadline:
        resp = client.get(f"/api/accounts/{account_id}")
        assert resp.status_code == 200, resp.text
        account = resp.json()
        last_state = account["state"]
        if last_state == "active":
            return account
        if last_state == "error":
            raise AssertionError(f"Account entered error state: {account['state_error']}")
        time.sleep(1)
    raise TimeoutError(
        f"Account {account_id} did not reach 'active' within {timeout_s}s "
        f"(last state: {last_state!r})"
    )


def wait_for_folder(
    client: httpx.Client, account_id: str, imap_name: str, timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll an account's folder list until one with the given imap_name appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/accounts/{account_id}/folders")
        assert resp.status_code == 200, resp.text
        for candidate in resp.json():
            if candidate["imap_name"] == imap_name:
                return candidate
        time.sleep(1)
    raise TimeoutError(f"Folder {imap_name!r} not discovered within {timeout_s}s")
