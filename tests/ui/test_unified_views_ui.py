"""
Unified views working like folders -- the same toolbar (quick filter,
conversation grouping, unread-only), one folder in several views, rows that
name their account once and make unread mail unmistakable -- and a mail
alert opening its message where it is now.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import time
import uuid
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    add_folder_to_unified_view,
    create_account,
    mail_row,
    select_account,
    select_unified_view,
    wait_for,
    wait_for_folder,
)
from tests.ui.test_mail_selection_unified_ui import (
    _create_folder,
    _open_unified_folder,
)


def _deliver(
    dovecot_endpoint: tuple[str, int, int],
    api_client: httpx.Client,
    account: dict[str, Any],
    folder_id: str,
    subject: str,
    *,
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> dict[str, Any]:
    host, _imap_port, lmtp_port = dovecot_endpoint
    eml = build_eml(
        sender="sender@example.com", recipient=account["email"], subject=subject,
        message_id=message_id or f"<{uuid.uuid4()}@example.com>", in_reply_to=in_reply_to,
    )
    deliver_message(eml, host, lmtp_port, sender="sender@example.com", recipient=account["email"])

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account['id']}/messages", params={"folder_id": folder_id},
        )
        assert resp.status_code == 200, resp.text
        return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)

    return wait_for(_find, timeout_s=45.0, description=f"{subject!r} synced")


def _move(api_client: httpx.Client, message_id: str, folder_id: str) -> None:
    resp = api_client.post(
        f"/api/messages/{message_id}/action",
        json={"action": "move", "target_folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text

    def _landed() -> bool | None:
        detail = api_client.get(f"/api/messages/{message_id}").json()
        return True if detail["folder_id"] == folder_id and not detail["pending_sync"] else None

    wait_for(_landed, timeout_s=30.0, description=f"{message_id} moved into {folder_id}")


def _mark_read(api_client: httpx.Client, message_id: str) -> None:
    resp = api_client.post(f"/api/messages/{message_id}/action", json={"action": "mark_read"})
    assert resp.status_code == 200, resp.text
    wait_for(
        lambda: True if api_client.get(f"/api/messages/{message_id}").json()["is_seen"] else None,
        timeout_s=20.0, description=f"{message_id} read",
    )


def _wait_for_view(api_client: httpx.Client, name: str, folder_count: int) -> dict[str, Any]:
    def _ready() -> dict[str, Any] | None:
        resp = api_client.get("/api/unified/folders")
        assert resp.status_code == 200, resp.text
        view = next((v for v in resp.json() if v["unified_name"] == name), None)
        return view if view is not None and len(view["folders"]) == folder_count else None

    return wait_for(
        _ready, timeout_s=30.0, description=f"unified view {name!r} with {folder_count} folders",
    )


def _run(coro: Coroutine[Any, Any, str]) -> str:
    # pytest-playwright holds a running loop on this thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _seed_alert(
    postgres_url: str, account_id: str, message_id: str, folder_id: str, title: str,
) -> str:
    """An undismissed mail alert for a message, the shape arrival produces."""

    async def _go() -> str:
        engine = create_async_engine(postgres_url)
        alert_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, kind, deliver_at, delivered_at, title, body, url, "
                    "account_id, message_id, folder_id, dedupe_key) "
                    "VALUES (:id, 'mail', now(), now(), :title, 'sender@example.com', "
                    ":url, :account_id, :message_id, :folder_id, :dedupe_key)"
                ),
                {
                    "id": alert_id, "title": title, "url": f"/?message={message_id}",
                    "account_id": uuid.UUID(account_id), "message_id": uuid.UUID(message_id),
                    "folder_id": uuid.UUID(folder_id), "dedupe_key": f"mail:test:{alert_id}",
                },
            )
        await engine.dispose()
        return str(alert_id)

    return _run(_go())


def _goto(page: Page, url: str) -> None:
    """Navigate and wait for the sidebar to hold a folder -- it only does
    once the static page has hydrated and loaded its data, so a click on
    the sidebar's own controls straight after load lands on live markup
    rather than on inert HTML."""
    page.goto(url)
    expect(page.locator('[data-testid="folder"]').first).to_be_visible(timeout=30_000)


def _open_alert(page: Page, alert_id: str) -> None:
    page.get_by_title("Notifications", exact=True).click()
    row = page.locator(f'[data-alert-id="{alert_id}"]')
    expect(row).to_be_visible(timeout=15_000)
    row.get_by_role("button").first.click()


def _group_switch(page: Page) -> Locator:
    return page.locator("label").filter(has_text="Group by conversation").get_by_role("switch")


def _unread_toggle(page: Page) -> Locator:
    return page.get_by_role("button", name="Show only unread messages", exact=True)


# Any CSS colour -- the theme's are oklch() -- as sRGB, through the
# browser's own canvas conversion.
_STYLE_JS = """(el) => {
  const rgb = (color) => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3);
  };
  const probe = document.createElement('div');
  probe.className = 'bg-background';
  document.body.append(probe);
  const background = rgb(getComputedStyle(probe).backgroundColor);
  probe.remove();
  const style = getComputedStyle(el);
  return {
    weight: parseInt(style.fontWeight, 10),
    size: parseFloat(style.fontSize),
    color: rgb(style.color),
    background,
  };
}"""


def _luminance(rgb: list[int]) -> float:
    def linear(channel: int) -> float:
        srgb = channel / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(c) for c in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(a: list[int], b: list[int]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class TestUnifiedViewToolbar:
    def test_filter_and_grouping_work_across_a_views_folders(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account_a = create_account(api_client, "uv-thread-a")
        account_b = create_account(api_client, "uv-thread-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")
        elsewhere_a = _create_folder(
            api_client, account_a["id"], f"Elsewhere-{uuid.uuid4().hex[:8]}",
        )

        view = f"Everything {uuid.uuid4().hex[:8]}"
        for folder_id in (inbox_a["id"], elsewhere_a["id"], inbox_b["id"]):
            add_folder_to_unified_view(api_client, folder_id, view)

        marker = uuid.uuid4().hex[:8]
        first_id = f"<first-{marker}@example.com>"
        first = _deliver(
            dovecot_endpoint, api_client, account_a, inbox_a["id"], f"Conversation {marker}",
            message_id=first_id,
        )
        answer = _deliver(
            dovecot_endpoint, api_client, account_a, inbox_a["id"], f"Re: Conversation {marker}",
            in_reply_to=first_id,
        )
        loner_token = f"lonertoken{uuid.uuid4().hex[:8]}"
        loner = _deliver(
            dovecot_endpoint, api_client, account_b, inbox_b["id"], f"Standalone {loner_token}",
        )
        assert answer["thread_id"] == first["thread_id"]
        # The conversation now spans two of the view's folders.
        _move(api_client, answer["id"], elsewhere_a["id"])
        _wait_for_view(api_client, view, 3)

        _goto(page, app_server)
        select_unified_view(page)
        _open_unified_folder(page, view)

        # Grouped (the default): one row for the conversation, counting both.
        answer_row = mail_row(page, answer["id"])
        expect(answer_row).to_be_visible(timeout=15_000)
        expect(answer_row.get_by_text("2", exact=True)).to_be_visible()
        expect(mail_row(page, loner["id"])).to_be_visible()
        with pytest.raises(AssertionError):
            expect(mail_row(page, first["id"])).to_be_visible(timeout=3_000)

        _group_switch(page).click()
        expect(mail_row(page, first["id"])).to_be_visible(timeout=15_000)
        expect(answer_row).to_be_visible()

        # By label, not placeholder -- the placeholder itself is now the
        # short "Filter…" so it never clips at the list's minimum width.
        page.get_by_label("Filter this view by subject, sender or recipient").fill(loner_token)
        expect(mail_row(page, loner["id"])).to_be_visible(timeout=15_000)
        expect(answer_row).not_to_be_visible(timeout=15_000)
        expect(mail_row(page, first["id"])).not_to_be_visible()


class TestUnreadOnly:
    def test_hides_read_mail_in_a_folder_and_in_a_unified_view(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account = create_account(api_client, "uv-unread")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        marker = uuid.uuid4().hex[:8]
        seen = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Seen {marker}")
        unseen = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Unseen {marker}")
        _mark_read(api_client, seen["id"])
        view = f"Unread check {marker}"
        add_folder_to_unified_view(api_client, inbox["id"], view)
        _wait_for_view(api_client, view, 1)

        _goto(page, app_server)
        select_account(page, account)

        def _check_toggle() -> None:
            seen_row, unseen_row = mail_row(page, seen["id"]), mail_row(page, unseen["id"])
            expect(seen_row).to_be_visible(timeout=15_000)
            expect(unseen_row).to_be_visible()
            toggle = _unread_toggle(page)
            toggle.click()
            expect(toggle).to_have_attribute("aria-pressed", "true")
            expect(seen_row).not_to_be_visible(timeout=15_000)
            expect(unseen_row).to_be_visible()
            toggle.click()
            expect(toggle).to_have_attribute("aria-pressed", "false")
            expect(seen_row).to_be_visible(timeout=15_000)
            expect(unseen_row).to_be_visible()

        _check_toggle()

        select_unified_view(page)
        _open_unified_folder(page, view)
        _check_toggle()


class TestOneFolderInSeveralViews:
    def test_a_folder_ticked_into_two_views_shows_its_mail_in_both(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account = create_account(api_client, "uv-multi")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        marker = uuid.uuid4().hex[:8]
        message = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Shared {marker}")
        views = []
        for label, emoji in ((f"Alpha {marker}", "🔵"), (f"Beta {marker}", "🟢")):
            resp = api_client.post("/api/unified/views", json={"name": label, "emoji": emoji})
            assert resp.status_code == 201, resp.text
            views.append(resp.json())

        page.goto(f"{app_server}/settings")
        folder_row = page.locator(
            f'[data-testid="unified-folder-row"][data-folder-id="{inbox["id"]}"]'
        )
        folder_row.get_by_role("button", name="Unified views for INBOX", exact=True).click()
        for view in views:
            # The multi-select stays open between ticks.
            page.get_by_role("menuitemcheckbox", name=view["name"], exact=True).click()
        page.keyboard.press("Escape")

        def _both_assigned() -> bool | None:
            folders = api_client.get(f"/api/accounts/{account['id']}/folders").json()
            ids = next(f for f in folders if f["id"] == inbox["id"])["unified_view_ids"]
            return True if set(ids) == {v["id"] for v in views} else None

        wait_for(_both_assigned, timeout_s=15.0, description="INBOX in both views")
        expect(folder_row.get_by_test_id("folder-view-chip")).to_have_count(2)

        _goto(page, app_server)
        select_unified_view(page)
        for view in views:
            sidebar_row = page.locator('[data-testid="folder"]').filter(has_text=view["name"])
            expect(sidebar_row.get_by_test_id("unified-view-emoji")).to_have_text(view["emoji"])
            _open_unified_folder(page, view["name"])
            expect(mail_row(page, message["id"])).to_be_visible(timeout=15_000)


class TestRows:
    def test_a_unified_row_names_its_account_once_and_shows_a_bold_smaller_subject(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account = create_account(api_client, "uv-row")
        resp = api_client.put(f"/api/accounts/{account['id']}/emoji", json={"emoji": "🦊"})
        assert resp.status_code == 200, resp.text
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        marker = uuid.uuid4().hex[:8]
        message = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Row {marker}")
        view = f"Row check {marker}"
        add_folder_to_unified_view(api_client, inbox["id"], view)
        _wait_for_view(api_client, view, 1)

        _goto(page, app_server)
        select_unified_view(page)
        _open_unified_folder(page, view)
        row = mail_row(page, message["id"])
        expect(row).to_be_visible(timeout=15_000)

        # The avatar badge and the sender line read the same account data,
        # so once the badge has rendered the sender line is final.
        expect(row.get_by_test_id("account-badge")).to_have_text("🦊")
        expect(row.locator('[data-slot="row-sender-line"]').get_by_text("🦊")).to_have_count(0)

        sender = row.locator('[data-slot="row-sender"]').evaluate(_STYLE_JS)
        subject = row.locator('[data-slot="row-subject"]').evaluate(_STYLE_JS)
        assert subject["weight"] >= 600, subject
        assert subject["size"] < sender["size"], (subject, sender)

    @pytest.mark.parametrize("scheme", ["dark", "light"])
    def test_unread_and_read_rows_differ_at_a_glance(
        self,
        browser: Browser,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        scheme: str,
    ) -> None:
        account = create_account(api_client, f"uv-contrast-{scheme}")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        marker = uuid.uuid4().hex[:8]
        seen = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Seen {marker}")
        unseen = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Unseen {marker}")
        _mark_read(api_client, seen["id"])

        context = browser.new_context(color_scheme=scheme, viewport={"width": 1440, "height": 900})
        try:
            page = context.new_page()
            _goto(page, app_server)
            html = page.locator("html")
            if scheme == "dark":
                expect(html).to_have_class(re.compile(r"\bdark\b"))
            else:
                expect(html).not_to_have_class(re.compile(r"\bdark\b"))
            select_account(page, account)

            unseen_row = mail_row(page, unseen["id"]).locator("[data-unread]")
            seen_row = mail_row(page, seen["id"]).locator("[data-unread]")
            expect(unseen_row).to_have_attribute("data-unread", "true", timeout=15_000)
            expect(seen_row).to_have_attribute("data-unread", "false", timeout=15_000)
            expect(unseen_row.get_by_test_id("unread-dot")).to_be_visible()
            expect(seen_row.get_by_test_id("unread-dot")).to_have_count(0)

            styles = {
                name: {
                    part: row.locator(f'[data-slot="row-{part}"]').evaluate(_STYLE_JS)
                    for part in ("sender", "subject")
                }
                for name, row in (("unseen", unseen_row), ("seen", seen_row))
            }
            assert styles["unseen"]["sender"]["weight"] >= 700, styles
            assert styles["seen"]["sender"]["weight"] <= 400, styles

            background = styles["unseen"]["subject"]["background"]
            unseen_contrast = _contrast(styles["unseen"]["subject"]["color"], background)
            seen_contrast = _contrast(styles["seen"]["subject"]["color"], background)
            assert unseen_contrast > 1.5 * seen_contrast, (unseen_contrast, seen_contrast)

            row_backgrounds = [
                row.evaluate("(el) => getComputedStyle(el).backgroundColor")
                for row in (unseen_row, seen_row)
            ]
            assert row_backgrounds[0] != row_backgrounds[1], row_backgrounds
        finally:
            context.close()


class TestUnreadOnlyKeepsReadRow:
    def test_a_message_read_while_visible_stays_until_navigation(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        account = create_account(api_client, "uv-unread-keep")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        marker = uuid.uuid4().hex[:8]
        target = _deliver(dovecot_endpoint, api_client, account, inbox["id"], f"Keep {marker}")

        _goto(page, app_server)
        select_account(page, account)
        toggle = _unread_toggle(page)
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.click()
        expect(page.get_by_role("heading", name=target["subject"], exact=True)).to_be_visible(
            timeout=10_000,
        )

        def _read() -> bool | None:
            detail = api_client.get(f"/api/messages/{target['id']}").json()
            return True if detail["is_seen"] else None

        wait_for(_read, description=f"{target['subject']!r} marked read on open")

        # Long enough for the window refresh that follows the mark-read
        # settling to have run -- a naive re-filter against is_seen=false
        # would drop this row right here.
        time.sleep(2.0)
        expect(row).to_be_visible()

        # Turning the filter off and back on is a fresh list -- unlike
        # leaving it open, this does not keep the row.
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(row).not_to_be_visible(timeout=15_000)


class TestKeyboardActionInUnifiedView:
    def test_trash_shortcut_moves_the_message_in_its_own_account(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        # Two accounts sharing one view -- a row's account is real, never
        # the pseudo-id "unified", so a keyboard action on it must resolve
        # against that one account's own Trash rather than falling back to
        # something that names no account at all.
        account_a = create_account(api_client, "uv-kbd-a")
        account_b = create_account(api_client, "uv-kbd-b")
        inbox_a = wait_for_folder(api_client, account_a["id"], "INBOX")
        inbox_b = wait_for_folder(api_client, account_b["id"], "INBOX")
        trash_a = wait_for_folder(api_client, account_a["id"], "Trash")
        marker = uuid.uuid4().hex[:8]
        target = _deliver(dovecot_endpoint, api_client, account_a, inbox_a["id"], f"Kbd {marker}")
        # A second account's own mail sits in the same list, so a wrong
        # fallback landing on the other account's folders would be visible.
        _deliver(dovecot_endpoint, api_client, account_b, inbox_b["id"], f"Other {marker}")
        view = f"Kbd check {marker}"
        for folder_id in (inbox_a["id"], inbox_b["id"]):
            add_folder_to_unified_view(api_client, folder_id, view)
        _wait_for_view(api_client, view, 2)

        _goto(page, app_server)
        select_unified_view(page)
        _open_unified_folder(page, view)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()
        expect(page.get_by_role("heading", name=target["subject"], exact=True)).to_be_visible(
            timeout=10_000,
        )

        page.keyboard.press("Delete")
        expect(row).not_to_be_visible(timeout=15_000)

        def _in_trash_a() -> bool | None:
            detail = api_client.get(f"/api/messages/{target['id']}").json()
            return True if detail["folder_id"] == trash_a["id"] else None

        wait_for(_in_trash_a, description=f"{target['subject']!r} moved to account A's Trash")


class TestAlertOpensTheMessageWhereItIsNow:
    def test_a_moved_message_opens_in_its_new_folder(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        postgres_url: str,
    ) -> None:
        account = create_account(api_client, "uv-alert-moved")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        elsewhere = _create_folder(api_client, account["id"], f"Elsewhere-{uuid.uuid4().hex[:8]}")
        subject = f"Alerted {uuid.uuid4().hex[:8]}"
        message = _deliver(dovecot_endpoint, api_client, account, inbox["id"], subject)
        alert_id = _seed_alert(postgres_url, account["id"], message["id"], inbox["id"], subject)
        _move(api_client, message["id"], elsewhere["id"])

        _goto(page, app_server)
        select_account(page, account)
        _open_alert(page, alert_id)

        expect(page).to_have_url(
            re.compile(rf"folder={elsewhere['id']}&message={message['id']}"), timeout=15_000,
        )
        expect(mail_row(page, message["id"])).to_be_visible(timeout=15_000)

    def test_it_opens_in_the_recent_unified_view_holding_its_folder(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        postgres_url: str,
    ) -> None:
        account = create_account(api_client, "uv-alert-view")
        inbox = wait_for_folder(api_client, account["id"], "INBOX")
        elsewhere = _create_folder(api_client, account["id"], f"Elsewhere-{uuid.uuid4().hex[:8]}")
        marker = uuid.uuid4().hex[:8]
        holding = f"Holding {marker}"
        latest = f"Latest {marker}"
        add_folder_to_unified_view(api_client, elsewhere["id"], holding)
        add_folder_to_unified_view(api_client, inbox["id"], latest)
        _wait_for_view(api_client, holding, 1)
        _wait_for_view(api_client, latest, 1)

        subject = f"Alerted {marker}"
        message = _deliver(dovecot_endpoint, api_client, account, inbox["id"], subject)
        alert_id = _seed_alert(postgres_url, account["id"], message["id"], inbox["id"], subject)
        _move(api_client, message["id"], elsewhere["id"])

        _goto(page, app_server)
        select_unified_view(page)
        _open_unified_folder(page, holding)
        expect(mail_row(page, message["id"])).to_be_visible(timeout=15_000)
        # The reader moves on to another view -- one that does not hold the
        # message's folder -- before the alert is clicked.
        _open_unified_folder(page, latest)
        expect(mail_row(page, message["id"])).not_to_be_visible(timeout=15_000)

        _open_alert(page, alert_id)

        expect(page).to_have_url(
            re.compile(rf"account=unified&folder=[^&]+&message={message['id']}"), timeout=15_000,
        )
        holding_button = (
            page.locator('[data-testid="folder"]').filter(has_text=holding)
            .locator('[data-slot="sidebar-menu-button"]')
        )
        # A state attribute: present (empty) when active, absent otherwise.
        expect(holding_button).to_have_attribute("data-active", re.compile(r"^(|true)$"))
        expect(mail_row(page, message["id"])).to_be_visible(timeout=15_000)
