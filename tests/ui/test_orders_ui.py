"""
Orders & tickets: the web screens for this feature -- the sidebar entry,
the list's five-things row, the two empty states, the scroll-stable "New
activity" pill, the scroll-anchor restore on Back, mail corrections, and
the accounts/settings surfaces (the per-account switch, the settings
page's JSON filter, and "Look through recent mail...").

Every order but one is seeded directly through orders/repository.py's own
functions inside a plain DB transaction, the same pattern
tests/e2e/test_spam_review.py uses to seed verdicts -- proving the screen
against controlled data rather than a real or fake model call, which
tests/pg/test_order_worker.py and the design's own real-model check cover
elsewhere. Only the catch-up test needs the model to actually run, so it
alone switches settings.ai.provider to "fake" (orders/fake.py) -- the
same role that provider plays for the classify stage, deterministic and
requiring no key.

Test functions run in file order and lean on that: the two empty-state
tests need to run before any order or enabled account exists, and every
later test shares one account switched on for orders (the
orders_switched_on fixture, first requested -- and so first applied --
partway through the file).

Fixture names avoid every word this screen also renders as a control
(Remove, Move, Merge, Delete, Rewrite) -- get_by_role(name=...) matches by
substring, and a fixture sharing a control's word collides with it under
strict mode (see this repo's own notes on that trap).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import select

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import OrderMail
from mail_verdict.orders import repository as orders_repo
from tests.e2e.helpers import wait_for
from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import create_account

pytestmark = pytest.mark.usefixtures("app_server")

_ROW_HEIGHT = 124


def _run_async(coro: Any) -> Any:
    """Run an async DB/event call from this sync Playwright test module --
    the same thread-pool-plus-asyncio.run() shape test_mail_list_live_ui.py's
    own seeding helpers use, since pytest-playwright's sync API keeps an
    asyncio loop "running" on the main thread for the test's duration and
    a bare asyncio.run() here would refuse to nest inside it."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _seed_order_async(
    postgres_url: str,
    *,
    account_id: uuid.UUID,
    merchant: str,
    subject: str,
    status: str,
    is_open: bool,
    mails: list[dict[str, Any]],
    icon: str,
    announce_created: bool,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    connection = DatabaseConnection(
        DatabaseConfig(url=postgres_url, pool_size=2, max_overflow=0, reserved_for_requests=0)
    )
    await connection.init()
    try:
        async with connection.session() as session:
            order_id = await orders_repo.create_order(session)
            for mail in mails:
                await orders_repo.attach_mail(
                    session, order_id=order_id, account_id=account_id, msg_key=mail["msg_key"],
                    message_id=mail.get("message_id"), thread_id=None, subject=mail["subject"],
                    from_addr=mail["from_addr"], received_at=mail["received_at"],
                    attached_by="ai",
                )
            await orders_repo.recompute_aggregates(session, order_id)
            summary = "\n".join(f"- {m['subject']}" for m in mails)
            await orders_repo.write_order_text(
                session, order_id, merchant=merchant, subject=subject, status=status,
                is_open=is_open, icon=icon, summary=summary, model="fake",
            )
            mail_result = await session.execute(
                select(OrderMail.msg_key, OrderMail.id).where(OrderMail.order_id == order_id)
            )
            keys_by_msg_key = {row.msg_key: row.id for row in mail_result.all()}
            await session.commit()

        if announce_created:
            from mail_verdict.api.events import broadcast_event, get_event_ring

            ring = get_event_ring()
            if ring is not None:
                await broadcast_event(
                    connection, ring, "order.updated",
                    {"order_id": str(order_id), "change": "created"},
                )
    finally:
        await connection.close()
    return order_id, keys_by_msg_key


def _seed_order(
    postgres_url: str,
    *,
    account_id: uuid.UUID,
    merchant: str,
    subject: str,
    status: str = "confirmed",
    is_open: bool = True,
    mails: list[dict[str, Any]] | None = None,
    icon: str = "package",
    announce_created: bool = False,
) -> tuple[str, dict[str, str]]:
    """One fully-written order (visible to GET /api/orders), with its
    mails attached and aggregates recomputed -- optionally announcing
    itself the way the worker's own write path does, for the live-update
    test. Returns (order_id, {msg_key: order_mails.id})."""
    if mails is None:
        now = datetime.now(UTC)
        mails = [{
            "msg_key": f"<seed-{uuid.uuid4()}@example.com>", "subject": subject,
            "from_addr": "shop@example.com", "received_at": now,
        }]
    order_id, keys = _run_async(_seed_order_async(
        postgres_url, account_id=account_id, merchant=merchant, subject=subject, status=status,
        is_open=is_open, mails=mails, icon=icon, announce_created=announce_created,
    ))
    return str(order_id), {k: str(v) for k, v in keys.items()}


def _inbox_id(api_client: httpx.Client, account: dict[str, Any]) -> str:
    resp = api_client.get(f"/api/accounts/{account['id']}/folders")
    assert resp.status_code == 200, resp.text
    for f in resp.json():
        if f["imap_name"] == "INBOX":
            return str(f["id"])
    raise AssertionError("INBOX folder not found")


def _deliver_and_sync(
    api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int], account: dict[str, Any],
    *, sender: str, subject: str,
) -> dict[str, Any]:
    """A real message, delivered over LMTP and waited for in the mirror --
    for a mail row that needs to actually resolve to "mailbox" (order
    membership keyed on a message_id an order_mails row with none of its
    own resolves to "gone" instead, per orders/locate.py)."""
    host, _imap_port, lmtp_port = dovecot_endpoint
    message = build_eml(
        sender=sender, recipient=account["email"], subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>",
    )
    deliver_message(message, host, lmtp_port, sender=sender, recipient=account["email"])

    def _synced() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account['id']}/messages",
            params={"folder_id": _inbox_id(api_client, account)},
        )
        assert resp.status_code == 200, resp.text
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    return wait_for(_synced, description=f"{subject!r} to sync")


@pytest.fixture(scope="module")
def orders_account(api_client: httpx.Client) -> dict[str, Any]:
    """One account, orders switched OFF -- the state
    test_no_account_enabled_shows_the_no_orders_yet_state needs, and every
    later test's mail/orders home. Its own module-scoped fixture rather
    than a plain call in a test body, so it exists exactly once regardless
    of test order."""
    return create_account(api_client, "orders")


@pytest.fixture(scope="module")
def orders_switched_on(api_client: httpx.Client, orders_account: dict[str, Any]) -> dict[str, Any]:
    """orders_account, with settings.orders.model set and its own
    orders_enabled switched on through the API -- the state every test
    after the two empty-state ones needs. Requested only from there on, so
    the "no account enabled" state is still true when
    test_no_account_enabled_shows_the_no_orders_yet_state runs first."""
    resp = api_client.put("/api/settings/orders", json={"data": {"model": "fake"}})
    assert resp.status_code == 200, resp.text
    resp = api_client.patch(
        f"/api/accounts/{orders_account['id']}", json={"orders_enabled": True},
    )
    assert resp.status_code == 200, resp.text
    return orders_account


def test_no_account_enabled_shows_the_no_orders_yet_state(
    page: Page, app_server: str, orders_account: dict[str, Any],
) -> None:
    # From an already-hydrated page, the same shape
    # test_navigation_ui.py's own sidebar-link tests use -- clicking a
    # sidebar link on the very first paint of "/" races React attaching
    # its handler to the (already visible) server-rendered markup.
    page.goto(f"{app_server}/accounts")
    expect(page.get_by_role("heading", name="Accounts", exact=True)).to_be_visible(timeout=15_000)

    footer = page.locator('[data-slot="sidebar-footer"]')
    # The sidebar is wrapped in ClientOnly (it reads localStorage), so it
    # renders nothing until a render after mount -- wait for a link that
    # is actually there before snapshotting the footer's own text, which
    # (unlike expect()) never retries on its own.
    orders_link = footer.get_by_role("link", name="Orders & tickets", exact=True)
    expect(orders_link).to_be_visible(timeout=15_000)
    names = footer.get_by_role("link").all_text_contents()
    assert names.index("Orders & tickets") == names.index("Contacts") + 1
    assert names.index("Spam review") == names.index("Orders & tickets") + 1

    orders_link.click()
    expect(page).to_have_url(re.compile(r"/orders$"))
    expect(page.get_by_text("No orders yet", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("Switch it on for", exact=False)).to_be_visible()
    open_accounts = page.get_by_role("link", name="Open accounts", exact=True)
    expect(open_accounts).to_be_visible(timeout=15_000)
    expect(open_accounts).to_have_attribute("href", "/accounts")


def test_sidebar_entry_and_a_row_shows_its_five_things(
    page: Page, app_server: str, postgres_url: str, orders_switched_on: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    order_id, _ = _seed_order(
        postgres_url, account_id=uuid.UUID(orders_switched_on["id"]),
        merchant="Nordlicht Keramik", subject="Handcrafted blue glazed vase",
        # Closed: the status pill still reads "shipped" (a non-empty status
        # always wins over the open/finished fallback), and this keeps the
        # later "Open" filter test's own empty state genuinely empty rather
        # than showing this row.
        status="shipped", is_open=False,
        mails=[
            {
                "msg_key": "<row-check-1@example.com>", "subject": "Handcrafted blue glazed vase",
                "from_addr": "orders@nordlicht.example", "received_at": now - timedelta(days=2),
            },
            {
                "msg_key": "<row-check-2@example.com>", "subject": "Your parcel is on its way",
                "from_addr": "orders@nordlicht.example", "received_at": now,
            },
        ],
    )

    page.goto(f"{app_server}/orders")
    row = page.locator(f'[data-testid="order-row"][data-order-id="{order_id}"]')
    expect(row).to_be_visible(timeout=15_000)
    expect(row.get_by_text("Nordlicht Keramik", exact=True)).to_be_visible()  # merchant
    expect(row.get_by_text("Handcrafted blue glazed vase", exact=True)).to_be_visible()  # subject
    expect(row.get_by_text("shipped", exact=True)).to_be_visible()  # status
    expect(row.get_by_text("2 mails", exact=False)).to_be_visible()  # count
    # summary start
    expect(row.get_by_text("Your parcel is on its way", exact=False)).to_be_visible()
    # first/last mail dates -- scoped to the date span's own class, since
    # "2 mails" also matches a digit-then-word pattern loosely
    expect(row.locator("span.tabular-nums")).to_be_visible()


def test_open_filter_shows_the_nothing_bundled_yet_state(
    page: Page, app_server: str, postgres_url: str, orders_switched_on: dict[str, Any],
) -> None:
    """A second, *closed* order -- "All" is non-empty (the previous test's
    row and this one), but "Open" has zero, which is the other empty
    state's own trigger regardless of what "All" holds."""
    _seed_order(
        postgres_url, account_id=uuid.UUID(orders_switched_on["id"]),
        merchant="Hafenklang Festival", subject="Festival ticket", status="used", is_open=False,
    )
    page.goto(f"{app_server}/orders")
    expect(page.locator('[data-testid="order-row"]').first).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Open", exact=True).click()
    expect(page.get_by_text("Nothing bundled yet", exact=True)).to_be_visible(timeout=15_000)


def test_new_order_does_not_move_rows_under_a_scrolled_reader(
    page: Page, app_server: str, postgres_url: str, orders_switched_on: dict[str, Any],
) -> None:
    account_id = uuid.UUID(orders_switched_on["id"])
    base = datetime.now(UTC) - timedelta(hours=1)
    order_ids: list[str] = []
    for i in range(10):
        oid, _ = _seed_order(
            postgres_url, account_id=account_id, merchant=f"Pill Shop {i}",
            subject=f"Pill order {i}", status="confirmed", is_open=True,
            mails=[{
                "msg_key": f"<pill-{i}-{uuid.uuid4()}@example.com>", "subject": f"Pill order {i}",
                "from_addr": "shop@example.com", "received_at": base - timedelta(minutes=i),
            }],
        )
        order_ids.append(oid)

    page.goto(f"{app_server}/orders")
    scroll = page.locator('[data-testid="orders-list-scroll"]')
    expect(page.locator('[data-testid="order-row"]').first).to_be_visible(timeout=15_000)

    # A real wheel gesture, not a scripted scrollTop write -- it fires the
    # native scroll event the onScroll handler needs to notice the reader
    # is no longer at the top (see this repo's own notes on driving
    # scrolling for real rather than faking the event).
    scroll.hover()
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(300)

    scroll_top = scroll.evaluate("(el) => el.scrollTop")
    assert scroll_top > 0, "the wheel gesture did not scroll the list"
    target_index = round(scroll_top / _ROW_HEIGHT)
    assert 0 < target_index < len(order_ids), f"unexpected scroll position: {scroll_top}"
    target_order_id = order_ids[target_index]
    target_row = page.locator(f'[data-testid="order-row"][data-order-id="{target_order_id}"]')
    expect(target_row).to_be_visible()

    before_box = target_row.bounding_box()
    assert before_box is not None

    new_order_id, _ = _seed_order(
        postgres_url, account_id=account_id, merchant="Fresh Arrival",
        subject="Fresh Arrival order", status="confirmed", is_open=True,
        mails=[{
            "msg_key": f"<pill-new-{uuid.uuid4()}@example.com>", "subject": "Fresh Arrival order",
            "from_addr": "shop@example.com", "received_at": datetime.now(UTC),
        }],
        announce_created=True,
    )

    pill = page.locator('[data-testid="orders-new-activity-pill"]')
    expect(pill).to_be_visible(timeout=15_000)

    after_box = target_row.bounding_box()
    assert after_box is not None
    assert target_row.get_attribute("data-order-id") == order_ids[target_index], (
        "the tracked row moved to a different order"
    )
    assert abs(after_box["y"] - before_box["y"]) < 3, (
        f"the tracked row moved on screen: {before_box['y']} -> {after_box['y']}"
    )

    pill.click()
    first_row = page.locator('[data-testid="order-row"]').first
    expect(first_row).to_have_attribute("data-order-id", new_order_id, timeout=15_000)
    expect(scroll).to_have_js_property("scrollTop", 0, timeout=5_000)


def test_opening_a_mail_from_an_order_reaches_the_mail_view(
    page: Page, app_server: str, postgres_url: str, api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int], orders_switched_on: dict[str, Any],
) -> None:
    """A minimal, isolated check of the hand-off to useOpenMessage -- kept
    separate from the scroll-restore test below (which injects its own
    anchor directly) so a finding here is not entangled with that
    mechanism."""
    account = orders_switched_on
    real = _deliver_and_sync(
        api_client, dovecot_endpoint, account, sender="orders@nordlicht.example",
        subject=f"Open-from-order check {uuid.uuid4().hex[:6]}",
    )
    order_id, keys = _seed_order(
        postgres_url, account_id=uuid.UUID(account["id"]), merchant="Nordlicht Keramik",
        subject="Open-from-order order",
        mails=[{
            "msg_key": f"<open-check-{uuid.uuid4()}@example.com>", "subject": real["subject"],
            "from_addr": "orders@nordlicht.example", "received_at": datetime.now(UTC),
            "message_id": uuid.UUID(real["id"]),
        }],
    )
    mail_key = next(iter(keys.values()))

    # Visit "/" first and let it settle, the way a real user always
    # arrives -- this app's own mail view is the entry point, and jumping
    # straight to /orders?order=... (skipping it) is the one thing this
    # test does that a real session never would.
    page.goto(app_server)
    expect(page.get_by_placeholder("Search mail…")).to_be_visible(timeout=15_000)

    page.goto(f"{app_server}/orders?order={order_id}")
    row = page.locator(f'[data-testid="order-mail-row"][data-mail-key="{mail_key}"]')
    expect(row).to_be_visible(timeout=15_000)
    row.get_by_text(real["subject"], exact=True).click()
    expect(page).not_to_have_url(re.compile(r"[?&]order="), timeout=20_000)


def test_scroll_position_restores_after_returning_to_an_order(
    page: Page, app_server: str, postgres_url: str, orders_switched_on: dict[str, Any],
) -> None:
    """The restore mechanism itself (order-scroll-anchor.ts's read-back,
    exercised through the real components) -- an anchor is written
    directly into sessionStorage, the same shape order-detail.tsx's own
    handleOpenMail writes just before handing off to useOpenMessage, and
    the page is loaded fresh against it. This is deliberately decoupled
    from the click-and-navigate-away flow that
    test_opening_a_mail_from_an_order_reaches_the_mail_view checks --
    two different mechanisms, proven independently."""
    now = datetime.now(UTC)
    mails = [
        {
            "msg_key": f"<anchor-filler-{i}-{uuid.uuid4()}@example.com>",
            "subject": f"Older note {i}", "from_addr": "orders@nordlicht.example",
            "received_at": now - timedelta(hours=8 - i),
        }
        for i in range(6)
    ]
    target_msg_key = f"<anchor-target-{uuid.uuid4()}@example.com>"
    mails.append({
        "msg_key": target_msg_key, "subject": "The targeted mail",
        "from_addr": "orders@nordlicht.example", "received_at": now,
    })
    order_id, keys = _seed_order(
        postgres_url, account_id=uuid.UUID(orders_switched_on["id"]), merchant="Nordlicht Keramik",
        subject="Handcrafted blue glazed vase", status="shipped", is_open=True, mails=mails,
    )
    target_key = keys[target_msg_key]

    page.goto(f"{app_server}/orders?order={order_id}")
    detail_scroll = page.locator('[data-testid="order-detail-scroll"]')
    target_row = page.locator(f'[data-testid="order-mail-row"][data-mail-key="{target_key}"]')
    expect(target_row).to_be_visible(timeout=15_000)

    # Scroll it away from the top first, so a restore to 0 wouldn't look
    # identical to no restore at all.
    detail_scroll.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(100)
    before = target_row.bounding_box()
    assert before is not None

    row_top = page.evaluate(
        """([mailKey]) => {
          const row = document.querySelector(
            `[data-testid="order-mail-row"][data-mail-key="${mailKey}"]`,
          );
          const container = document.querySelector('[data-testid="order-detail-scroll"]');
          return row.getBoundingClientRect().top - container.getBoundingClientRect().top;
        }""",
        [target_key],
    )
    page.evaluate(
        """([anchor]) => sessionStorage.setItem("mv.orders.anchor", JSON.stringify(anchor))""",
        [{
            "orderId": order_id, "mailKey": target_key, "rowTop": row_top,
            "listIndex": 0, "listRowTop": 0,
        }],
    )

    page.goto(f"{app_server}/orders?order={order_id}")
    target_row_after = page.locator(
        f'[data-testid="order-mail-row"][data-mail-key="{target_key}"]',
    )
    expect(target_row_after).to_be_visible(timeout=15_000)
    page.wait_for_timeout(200)  # the hold's ResizeObserver settling
    after = target_row_after.bounding_box()
    assert after is not None
    assert abs(after["y"] - before["y"]) < 5, (
        f"the mail row did not return to the same screen position: {before['y']} -> {after['y']}"
    )


def test_taking_a_mail_out_of_an_order_lowers_its_count(
    page: Page, app_server: str, postgres_url: str, api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int], orders_switched_on: dict[str, Any],
) -> None:
    account = orders_switched_on
    # Real, delivered messages -- an order_mails row with no message_id of
    # its own resolves to "gone" (orders/locate.py), and a "gone" mail
    # carries no row menu at all, so detaching it is not a UI interaction
    # this app offers.
    confirmation = _deliver_and_sync(
        api_client, dovecot_endpoint, account, sender="shop@example.com",
        subject=f"Confirmation {uuid.uuid4().hex[:6]}",
    )
    shipping = _deliver_and_sync(
        api_client, dovecot_endpoint, account, sender="shop@example.com",
        subject=f"Shipping notice {uuid.uuid4().hex[:6]}",
    )
    now = datetime.now(UTC)
    order_id, keys = _seed_order(
        postgres_url, account_id=uuid.UUID(account["id"]), merchant="Two Mail Shop",
        subject="Two mail order", status="confirmed", is_open=True,
        mails=[
            {
                "msg_key": f"<twomail-a-{uuid.uuid4()}@example.com>",
                "subject": confirmation["subject"], "from_addr": "shop@example.com",
                "received_at": now - timedelta(hours=1),
                "message_id": uuid.UUID(confirmation["id"]),
            },
            {
                "msg_key": f"<twomail-b-{uuid.uuid4()}@example.com>",
                "subject": shipping["subject"], "from_addr": "shop@example.com",
                "received_at": now, "message_id": uuid.UUID(shipping["id"]),
            },
        ],
    )
    detach_key = next(k for msg_key, k in keys.items() if msg_key.startswith("<twomail-a-"))

    page.goto(f"{app_server}/orders?order={order_id}")
    row = page.locator(f'[data-testid="order-mail-row"][data-mail-key="{detach_key}"]')
    expect(row).to_be_visible(timeout=15_000)
    action_name = f"Actions for {confirmation['subject']}"
    row.get_by_role("button", name=action_name, exact=True).click()
    page.get_by_role("menuitem", name="Remove from this order", exact=True).click()

    expect(row).to_have_count(0, timeout=15_000)
    detail_pane = page.locator('[data-testid="order-detail-scroll"]')
    expect(detail_pane.get_by_text("1 mail", exact=False)).to_be_visible(timeout=15_000)


def test_account_form_orders_switch_and_settings_filter_json(
    page: Page, app_server: str, api_client: httpx.Client, orders_switched_on: dict[str, Any],
) -> None:
    fresh_account = create_account(api_client, "orders-toggle")

    page.goto(f"{app_server}/accounts")
    options_button = page.get_by_role("button", name=f"{fresh_account['name']} options", exact=True)
    expect(options_button).to_be_visible(timeout=15_000)
    options_button.click()
    page.get_by_role("menuitem", name="Edit", exact=True).click()

    checkbox = page.get_by_label("Bundle orders and tickets", exact=True)
    expect(checkbox).not_to_be_checked()
    checkbox.check()
    page.get_by_role("button", name="Update", exact=True).click()

    def _account_now() -> dict[str, Any] | None:
        resp = api_client.get(f"/api/accounts/{fresh_account['id']}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        return data if data["orders_enabled"] else None

    wait_for(_account_now, description="orders_enabled to persist")

    page.reload()
    options_button = page.get_by_role("button", name=f"{fresh_account['name']} options", exact=True)
    expect(options_button).to_be_visible(timeout=15_000)
    options_button.click()
    page.get_by_role("menuitem", name="Edit", exact=True).click()
    checkbox_after_reload = page.get_by_label("Bundle orders and tickets", exact=True)
    expect(checkbox_after_reload).to_be_checked()
    page.get_by_role("button", name="Cancel", exact=True).click()

    page.goto(f"{app_server}/settings")
    page.get_by_role("tab", name="Orders", exact=True).click()
    filter_field = page.get_by_text("First filter (patterns)", exact=True).locator(
        "xpath=following-sibling::textarea",
    )
    expect(filter_field).to_be_visible(timeout=15_000)

    current_filter = api_client.get("/api/settings/orders").json()["filter"]
    edited_filter = json.loads(json.dumps(current_filter))  # a plain, independent dict copy
    edited_filter.setdefault("include", {}).setdefault("subject", [])
    edited_filter["include"]["subject"] = [
        *edited_filter["include"]["subject"], "kind_of_test_marker_pattern",
    ]
    filter_field.fill(json.dumps(edited_filter, indent=2))
    page.get_by_role("button", name="Save", exact=True).click()

    def _filter_saved() -> dict[str, Any] | None:
        resp = api_client.get("/api/settings/orders")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        patterns = data["filter"]["include"]["subject"]
        return data if "kind_of_test_marker_pattern" in patterns else None

    wait_for(_filter_saved, description="the edited filter to be read back through the API")


def test_catch_up_preview_then_start_creates_orders_from_mail_already_there(
    page: Page, app_server: str, api_client: httpx.Client, dovecot_endpoint: tuple[str, int, int],
    orders_switched_on: dict[str, Any],
) -> None:
    account = orders_switched_on
    resp = api_client.put("/api/settings/ai", json={"data": {"provider": "fake"}})
    assert resp.status_code == 200, resp.text

    host, _imap_port, lmtp_port = dovecot_endpoint
    subject = f"Order confirmation {uuid.uuid4().hex[:8]}"
    message = build_eml(
        sender="shop@catchup.example", recipient=account["email"], subject=subject,
        message_id=f"<catchup-{uuid.uuid4()}@example.com>",
    )
    deliver_message(
        message, host, lmtp_port, sender="shop@catchup.example", recipient=account["email"],
    )

    def _synced() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account['id']}/messages",
            params={"folder_id": _inbox_id(api_client, account)},
        )
        assert resp.status_code == 200, resp.text
        for m in resp.json()["messages"]:
            if m["subject"] == subject:
                return m
        return None

    wait_for(_synced, description="the catch-up candidate message to sync")

    orders_before = api_client.get("/api/orders", params={"limit": 500}).json()["items"]

    page.goto(f"{app_server}/accounts")
    expand_button = page.get_by_role("button", name=f"Expand {account['name']}", exact=True)
    expect(expand_button).to_be_visible(timeout=15_000)
    expand_button.click()

    page.get_by_role("button", name="Look through recent mail…", exact=True).click()
    days_field = page.get_by_label("Days", exact=True)
    days_field.fill("30")
    page.get_by_role("button", name="Preview", exact=True).click()
    preview_text = page.get_by_text("would be read by the model", exact=False)
    expect(preview_text).to_be_visible(timeout=15_000)

    orders_after_preview = api_client.get("/api/orders", params={"limit": 500}).json()["items"]
    assert len(orders_after_preview) == len(orders_before), "a dry run must not create anything"

    page.get_by_role("button", name="Start", exact=True).click()
    expect(page.get_by_text("queued", exact=False)).to_be_visible(timeout=15_000)

    def _order_appeared() -> dict[str, Any] | None:
        resp = api_client.get("/api/orders", params={"limit": 500})
        assert resp.status_code == 200, resp.text
        for item in resp.json()["items"]:
            if subject.split()[0] in item["title"] and subject.split()[1] in item["title"]:
                return item
        return None

    wait_for(
        _order_appeared, timeout_s=45.0,
        description="the catch-up sweep to produce a written order",
    )


def test_phone_width_shows_list_then_detail_then_back(
    page: Page, app_server: str, postgres_url: str, orders_switched_on: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    order_id, _ = _seed_order(
        postgres_url, account_id=uuid.UUID(orders_switched_on["id"]), merchant="Phone Shop",
        subject="Phone width order",
        # A distinct mail subject -- the default single-mail seed reuses
        # the order's own subject, which then matches twice (the row and
        # the detail pane's own mail row) under a strict-mode locator.
        mails=[{
            "msg_key": f"<phone-{uuid.uuid4()}@example.com>", "subject": "Phone width mail",
            "from_addr": "shop@example.com", "received_at": now,
        }],
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{app_server}/orders")

    row = page.locator(f'[data-testid="order-row"][data-order-id="{order_id}"]')
    expect(row).to_be_visible(timeout=15_000)
    row.click()

    back_button = page.get_by_role("button", name="Orders", exact=True)
    expect(back_button).to_be_visible(timeout=15_000)
    expect(page.get_by_role("heading", name="Phone width order", exact=True)).to_be_visible(
        timeout=15_000,
    )
    expect(row).to_have_count(0)  # the list pane is not rendered while the detail is showing

    back_button.click()
    expect(row).to_be_visible(timeout=15_000)
