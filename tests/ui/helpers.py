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

import re
import time
from typing import Any

import httpx
from playwright.sync_api import Locator, Page, expect

from tests.e2e.helpers import (  # noqa: F401 -- re-exported for tests/ui/ callers
    unique_email,
    wait_for,
    wait_for_mailpit_message,
)

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
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


def folder_button(page: Page, folder_id: str) -> Locator:
    """The sidebar folder's own button. The options menu beside it is a
    button too, inside the same item, so asking the item for its button
    resolves to two and fails strict mode -- which reads as the folder
    never having rendered."""
    return folder(page, folder_id).locator('[data-slot="sidebar-menu-button"]')


def event_chip(page: Page, object_id: str) -> Locator:
    """The clickable calendar event chip, by its object id -- month cell,
    time grid block or agenda row, whichever view is rendering it.

    A recurring series renders one chip per visible occurrence sharing this
    same object id, so this matches more than one element for it -- use
    event_occurrence_chip() instead once occurrences are involved."""
    return page.locator(f'[data-testid="event"][data-event-id="{object_id}"]')


def event_occurrence_chip(page: Page, object_id: str, recurrence_id: str) -> Locator:
    """One specific occurrence of a (possibly recurring) event, by both the
    object id every occurrence shares and the recurrence id that is unique
    to this one."""
    return page.locator(
        f'[data-testid="event"][data-event-id="{object_id}"][data-recurrence-id="{recurrence_id}"]'
    )


def center_in_grid_viewport(page: Page, chip: Locator) -> None:
    """Scroll the time grid so the given chip sits in the middle of the
    scrollable viewport, leaving headroom on both sides for a pointer drag
    that starts on it -- scroll_into_view_if_needed() alone can leave it
    flush against an edge, with no room to drag further that way."""
    chip_id = chip.evaluate("(el) => el.getAttribute('data-event-id')")
    recurrence_id = chip.evaluate("(el) => el.getAttribute('data-recurrence-id')")
    page.evaluate(
        """([objectId, recurrenceId]) => {
            const chip = document.querySelector(
                `[data-testid="event"][data-event-id="${objectId}"][data-recurrence-id="${recurrenceId}"]`,
            );
            const scroller = document.querySelector('[data-testid="time-grid-scroll"]');
            const chipTop = chip.getBoundingClientRect().top
                - scroller.getBoundingClientRect().top + scroller.scrollTop;
            scroller.scrollTop = Math.max(0, chipTop - scroller.clientHeight / 2);
        }""",
        [chip_id, recurrence_id],
    )


def drag_by_pixels(page: Page, from_x: float, from_y: float, to_x: float, to_y: float) -> None:
    """A raw pointer drag between two viewport-relative points -- the time
    grid's drag hook listens for pointer events directly rather than
    through dnd-kit, so unlike drag_row_to_folder() this needs no
    activation-distance move first, only real intermediate pointermoves."""
    steps = 12
    page.mouse.move(from_x, from_y)
    page.mouse.down()
    for step in range(1, steps + 1):
        fraction = step / steps
        page.mouse.move(
            from_x + (to_x - from_x) * fraction, from_y + (to_y - from_y) * fraction,
        )
    page.mouse.up()


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


# select_account's own click is this module's repeat offender under host
# load: Playwright's 30s default action timeout, applied to the dropdown
# item, has been the most common cause of an otherwise-unrelated test
# failing partway through its own setup -- a load failure that then reads
# as whatever behaviour the test happened to be checking. A longer,
# explicit budget here is the fix, not a shorter one downstream: every
# later step in a test depends on this one succeeding.
_SELECT_ACCOUNT_TIMEOUT_MS = 60_000


# The switcher's label in the static markup, before the client has rendered
# anything; once hydrated it names an account or the unified view instead.
# A click landing before hydration opens nothing, silently.
_SWITCHER_UNHYDRATED_LABEL = re.compile(r"Select Account")
# One attempt at opening the switcher's menu waits this long before the
# next; a click that raced a re-render is retried rather than waited on.
_SWITCHER_OPEN_TIMEOUT_MS = 5_000
_SWITCHER_OPEN_ATTEMPTS = 4


def _choose_in_account_switcher(page: Page, entry: str) -> None:
    """Pick one entry -- an account's name or "Unified View" -- from the
    sidebar's account switcher.

    Waits for the switcher to have hydrated, then opens its menu, retrying
    an open that does not take, and fails naming which of the two never
    happened: the menu not opening at all, or opening without the entry.

    On a mobile viewport the switcher lives inside the sidebar's own sheet,
    closed by default -- the same hamburger trigger a phone user taps opens
    it first, and picking an entry leaves it open covering the page, so
    it's closed again the same way a person would dismiss it."""
    trigger = page.locator('[data-slot="sidebar-header"]').get_by_role("button").first
    expect(trigger).not_to_have_text(_SWITCHER_UNHYDRATED_LABEL, timeout=_SELECT_ACCOUNT_TIMEOUT_MS)
    opened_sheet = trigger.is_hidden()
    if opened_sheet:
        page.locator('[data-slot="sidebar-trigger"]').click()
        expect(trigger).to_be_visible(timeout=10_000)

    menu = page.locator('[data-slot="dropdown-menu-content"]')
    for _ in range(_SWITCHER_OPEN_ATTEMPTS):
        trigger.click()
        try:
            expect(menu).to_be_visible(timeout=_SWITCHER_OPEN_TIMEOUT_MS)
            break
        except AssertionError:
            page.keyboard.press("Escape")
    else:
        raise AssertionError(
            f"the account switcher's menu never opened in {_SWITCHER_OPEN_ATTEMPTS} "
            f"attempts (switcher reads {trigger.inner_text()!r}), so {entry!r} could "
            f"not be chosen"
        )

    item = menu.locator('[data-slot="dropdown-menu-item"]').get_by_text(entry, exact=True)
    try:
        expect(item).to_be_visible(timeout=_SELECT_ACCOUNT_TIMEOUT_MS)
    except AssertionError as exc:
        raise AssertionError(
            f"the account switcher opened but never listed {entry!r}; it lists "
            f"{menu.locator('[data-slot=\"dropdown-menu-item\"]').all_inner_texts()!r}"
        ) from exc
    item.click(timeout=_SELECT_ACCOUNT_TIMEOUT_MS)
    if opened_sheet:
        sheet = page.locator('[data-slot="sheet-portal"]')
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0, timeout=10_000)


def select_account(page: Page, account: dict[str, Any]) -> None:
    """Explicitly choose an account through the sidebar's own switcher.

    A fresh page load auto-selects whichever account sorts first by name
    across every account the shared test database currently holds -- not
    necessarily this test's own account, once other modules running in the
    same session have created accounts of their own. This drives the
    identical control a real multi-account user reaches for, rather than
    assuming the default lands on the right one."""
    _choose_in_account_switcher(page, account["name"])


def select_unified_view(page: Page) -> None:
    """Switch the sidebar to the unified views, through the same switcher
    select_account() drives."""
    _choose_in_account_switcher(page, "Unified View")


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


def create_account(client: httpx.Client, name_prefix: str) -> dict[str, Any]:
    """An active account wired to Mailpit, under a unique name -- the same
    shape several test modules each rebuilt by hand (mail actions, unified
    selection). Waits for `active` before returning, so a caller never
    races PostIMAP's own first connection. The account's own email is
    folded into the returned dict as `email`, matching what those callers
    already relied on."""
    email = unique_email(name_prefix)
    resp = client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "smtp_host": MAILPIT_ALIAS,
            "smtp_port": MAILPIT_SMTP_PORT,
            "smtp_user": email,
            "smtp_password": "unused",  # Mailpit accepts any SMTP AUTH credentials
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_account_active(client, account["id"])
    account["email"] = email
    return account


def add_folder_to_unified_view(client: httpx.Client, folder_id: str, view_name: str) -> str:
    """Put a folder into the unified view with this name, creating the view
    if it does not exist yet, and keep whatever other views the folder is
    already in. Returns the view's id."""
    resp = client.get("/api/unified/folders")
    assert resp.status_code == 200, resp.text
    views = resp.json()
    view = next((v for v in views if v["unified_name"] == view_name), None)
    if view is None:
        created = client.post("/api/unified/views", json={"name": view_name})
        assert created.status_code == 201, created.text
        view_id = created.json()["id"]
    else:
        view_id = view["id"]
    current = [v["id"] for v in views if any(f["folder_id"] == folder_id for f in v["folders"])]
    resp = client.patch(
        f"/api/folders/{folder_id}/prefs",
        json={"unified_view_ids": [*dict.fromkeys([*current, view_id])]},
    )
    assert resp.status_code == 200, resp.text
    return str(view_id)


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
