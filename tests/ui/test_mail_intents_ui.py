"""
Mail actions as the reader experiences them on a slow or absent network:
shown the moment they are taken, kept across a reload until the server has
them, sent exactly once, visibly failed when the server refuses, and taken
back with Ctrl+Z.

Every held-back request here is held by a route that is released by hand
rather than one that sleeps: a sleeping route handler blocks Playwright's
own dispatch, and with it the assertions meant to run while it is held. And
a route handler only ever runs while the test is inside a Playwright call,
so any wait for what a routed request does polls through the page
(_wait_through_page) rather than sleeping -- a plain sleep leaves the
request parked in the browser, unanswered, until it times out.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.setup.mail_delivery import build_eml, deliver_message
from tests.ui.helpers import (
    add_folder_to_unified_view,
    create_account,
    folder_button,
    mail_row,
    select_account,
    select_unified_view,
    wait_for,
    wait_for_folder,
)
from tests.ui.test_mail_selection_unified_ui import _create_folder, _open_unified_folder

_ACTION_ROUTE = "**/api/messages/*/action"
_LEDGER_KEY = "mail-verdict-mail-intents"


@pytest.fixture(scope="module")
def account(api_client: httpx.Client) -> dict[str, Any]:
    """An account with an Archive folder -- the server resolves archive
    through folder_prefs' special_use override, which Dovecot does not
    advertise on its own."""
    created = create_account(api_client, "intents")
    archive = _create_folder(api_client, created["id"], f"Archive-{uuid.uuid4().hex[:6]}")
    resp = api_client.patch(
        f"/api/folders/{archive['id']}/prefs", json={"special_use_override": "archive"},
    )
    assert resp.status_code == 200, resp.text
    created["inbox"] = wait_for_folder(api_client, created["id"], "INBOX")
    created["archive"] = archive
    created["elsewhere"] = _create_folder(
        api_client, created["id"], f"Elsewhere-{uuid.uuid4().hex[:6]}",
    )
    return created


def _move_elsewhere(api_client: httpx.Client, message_id: str, folder_id: str) -> None:
    """Another client filing the message: a write this browser never made."""
    resp = api_client.post(
        f"/api/messages/{message_id}/action",
        json={"action": "move", "target_folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text


def _deliver(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    account: dict[str, Any],
    subject: str,
) -> dict[str, Any]:
    host, _imap_port, lmtp_port = dovecot_endpoint
    eml = build_eml(
        sender="sender@example.com", recipient=account["email"], subject=subject,
        message_id=f"<{uuid.uuid4()}@example.com>",
    )
    deliver_message(eml, host, lmtp_port, sender="sender@example.com", recipient=account["email"])

    def _find() -> dict[str, Any] | None:
        resp = api_client.get(
            f"/api/accounts/{account['id']}/messages", params={"folder_id": account["inbox"]["id"]},
        )
        assert resp.status_code == 200, resp.text
        return next((m for m in resp.json()["messages"] if m["subject"] == subject), None)

    # A delivery right after a move on the same account can take PostIMAP most
    # of a minute to pick up.
    return wait_for(_find, timeout_s=90.0, description=f"{subject!r} synced into INBOX")


def _folder_of(api_client: httpx.Client, message_id: str) -> str:
    return str(api_client.get(f"/api/messages/{message_id}").json()["folder_id"])


def _wait_in_folder(
    api_client: httpx.Client, message_id: str, folder_id: str, what: str, timeout_s: float = 20.0,
) -> None:
    wait_for(
        lambda: True if _folder_of(api_client, message_id) == folder_id else None,
        timeout_s=timeout_s, description=what,
    )


def _wait_through_page(
    page: Page, api_client: httpx.Client, message_id: str, folder_id: str, what: str,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while _folder_of(api_client, message_id) != folder_id:
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {timeout_s}s")
        page.wait_for_timeout(250)


def _wait_seen(api_client: httpx.Client, message_id: str) -> None:
    wait_for(
        lambda: True if api_client.get(f"/api/messages/{message_id}").json()["is_seen"] else None,
        timeout_s=20.0, description=f"{message_id} marked read on open",
    )


def _open_inbox(page: Page, app_server: str, account: dict[str, Any]) -> None:
    page.goto(app_server)
    select_account(page, account)
    folder_button(page, account["inbox"]["id"]).click()


class _HeldRoute:
    """Holds every matching request until released, recording each body."""

    def __init__(self) -> None:
        self.held: list[Route] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, route: Route, request: Request) -> None:
        if request.method == "POST":
            self.bodies.append(json.loads(request.post_data or "{}"))
        self.held.append(route)

    def release(self) -> None:
        for route in self.held:
            route.continue_()
        self.held.clear()


class TestActionsShowAtOnce:
    def test_archiving_the_last_message_of_a_unified_view_removes_it_before_the_server_answers(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """A unified view's list is its own cache. Archiving its only message
        used to leave the row on screen until a refresh -- the pane moved on,
        the list did not."""
        view = f"Intents {uuid.uuid4().hex[:8]}"
        add_folder_to_unified_view(api_client, account["inbox"]["id"], view)
        target = _deliver(api_client, dovecot_endpoint, account, f"Last one {uuid.uuid4()}")

        page.goto(app_server)
        select_unified_view(page)
        _open_unified_folder(page, view)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()
        expect(page.get_by_role("heading", name=target["subject"], exact=True)).to_be_visible(
            timeout=10_000,
        )
        _wait_seen(api_client, target["id"])

        hold = _HeldRoute()
        page.route(_ACTION_ROUTE, hold)
        try:
            page.get_by_role("toolbar", name="Message actions").get_by_title(
                "Archive", exact=True,
            ).click()
            expect(row).not_to_be_visible(timeout=1_000)
            expect(page.get_by_text("Select a message to read")).to_be_visible(timeout=1_000)
            deadline = time.monotonic() + 5.0
            while not hold.held and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert hold.held, "the archive request was never sent"
            assert _folder_of(api_client, target["id"]) == account["inbox"]["id"]
        finally:
            hold.release()
            page.unroute(_ACTION_ROUTE, hold)

        _wait_through_page(
            page, api_client, target["id"], account["archive"]["id"], "archived", 20.0,
        )
        # The refresh that follows the answer agrees; the row stays gone.
        page.wait_for_timeout(2_000)
        expect(row).to_have_count(0)

    def test_a_slow_star_shows_at_once_and_marks_the_row_pending(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        target = _deliver(api_client, dovecot_endpoint, account, f"Slow star {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        hold = _HeldRoute()
        page.route(_ACTION_ROUTE, hold)
        try:
            row.hover()
            row.get_by_title("Star", exact=True).click()
            expect(row.get_by_title("Unstar", exact=True)).to_be_visible(timeout=1_000)
            expect(row.locator('[data-testid="row-action-pending"]')).to_be_visible(timeout=3_000)
        finally:
            hold.release()
            page.unroute(_ACTION_ROUTE, hold)

        expect(row.locator('[data-testid="row-action-pending"]')).not_to_be_visible(timeout=10_000)
        expect(row.get_by_title("Unstar", exact=True)).to_be_visible()
        assert hold.bodies and uuid.UUID(hold.bodies[0]["idempotency_key"])


class TestActionsSurviveTheNetwork:
    def test_an_offline_archive_is_kept_across_a_reload_and_sent_exactly_once(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        target = _deliver(api_client, dovecot_endpoint, account, f"Offline {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        page.context.set_offline(True)
        row.hover()
        row.get_by_title("Archive").click()
        expect(row).not_to_be_visible(timeout=1_000)
        expect(page.get_by_test_id("actions-waiting")).to_contain_text(
            "1 action waiting for the network", timeout=5_000,
        )

        stored = json.loads(page.evaluate(f"localStorage.getItem('{_LEDGER_KEY}')"))
        pending = [i for i in stored["intents"] if i["state"] in ("pending", "inflight")]
        assert [i["action"] for i in pending] == ["archive"], stored["intents"]
        key = pending[0]["id"]

        # Back online for page loads, while every action request still fails
        # the way a dead connection does -- so nothing reaches the server
        # before the reload.
        def _drop(route: Route) -> None:
            route.abort("internetdisconnected")

        page.route(_ACTION_ROUTE, _drop)
        page.context.set_offline(False)
        page.reload()
        expect(page.locator('[data-testid="folder"]').first).to_be_visible(timeout=30_000)
        assert _folder_of(api_client, target["id"]) == account["inbox"]["id"]

        sent: list[dict[str, Any]] = []

        def _count(route: Route, request: Request) -> None:
            sent.append(json.loads(request.post_data or "{}"))
            route.continue_()

        page.unroute(_ACTION_ROUTE, _drop)
        page.route(_ACTION_ROUTE, _count)
        try:
            # Retries back off while the connection is dead, at most ten
            # seconds apart.
            _wait_through_page(
                page, api_client, target["id"], account["archive"]["id"], "archived after reload",
                30.0,
            )
            page.wait_for_timeout(3_000)  # room for a duplicate, were one coming
        finally:
            page.unroute(_ACTION_ROUTE, _count)

        archives = [body for body in sent if body.get("action") == "archive"]
        assert [body["idempotency_key"] for body in archives] == [key], sent
        select_account(page, account)
        folder_button(page, account["inbox"]["id"]).click()
        expect(page.get_by_test_id("actions-waiting")).to_have_count(0, timeout=10_000)
        expect(mail_row(page, target["id"])).to_have_count(0, timeout=10_000)

    def test_a_refused_action_offers_retry_and_discard(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        """No Archive folder: the server refuses, the row comes back saying
        so, and nothing is offered as undoable."""
        bare = create_account(api_client, "intents-bare")
        bare["inbox"] = wait_for_folder(api_client, bare["id"], "INBOX")
        target = _deliver(api_client, dovecot_endpoint, bare, f"Refused {uuid.uuid4()}")
        _open_inbox(page, app_server, bare)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        row.hover()
        row.get_by_title("Archive").click()

        refusal = "Could not archive: No archive folder found for this account"
        expect(page.get_by_text(refusal, exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name="Undo", exact=True)).to_have_count(0)
        expect(row).to_be_visible()
        chip = row.locator('[data-slot="row-action-failed"]')
        expect(chip).to_contain_text("Could not archive")
        expect(chip.get_by_role("button", name="Retry", exact=True)).to_be_visible()

        chip.get_by_role("button", name="Discard", exact=True).click()
        expect(chip).to_have_count(0, timeout=5_000)
        expect(row).to_be_visible()
        assert _folder_of(api_client, target["id"]) == bare["inbox"]["id"]


class TestUndoKey:
    def test_ctrl_z_after_archive_brings_the_message_back(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        target = _deliver(api_client, dovecot_endpoint, account, f"Take back {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()
        _wait_seen(api_client, target["id"])

        page.keyboard.press("e")
        expect(row).not_to_be_visible(timeout=1_000)
        _wait_in_folder(api_client, target["id"], account["archive"]["id"], "archived")

        page.locator("body").click(position={"x": 1, "y": 1})
        page.keyboard.press("Control+z")
        expect(mail_row(page, target["id"])).to_be_visible(timeout=5_000)
        _wait_in_folder(api_client, target["id"], account["inbox"]["id"], "moved back by undo")

    def test_ctrl_z_while_typing_in_the_composer_leaves_mail_alone(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        target = _deliver(api_client, dovecot_endpoint, account, f"Stay archived {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()
        row.get_by_title("Archive").click()
        _wait_in_folder(api_client, target["id"], account["archive"]["id"], "archived")

        page.locator("body").click(position={"x": 1, "y": 1})
        page.keyboard.press("c")
        subject = page.get_by_placeholder("Subject")
        expect(subject).to_be_visible(timeout=10_000)
        subject.fill("Draft words")
        page.keyboard.press("Control+z")

        with pytest.raises(AssertionError):
            expect(page.get_by_text("Undone:", exact=False)).to_be_visible(timeout=3_000)
        assert _folder_of(api_client, target["id"]) == account["archive"]["id"]


class TestRulings:
    def test_confirming_a_verdict_that_moves_nothing_keeps_the_message_open(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """A not-spam verdict confirmed: the server records it and leaves the
        message where it is, so the reader stays on it and there is nothing
        to undo."""
        target = _deliver(api_client, dovecot_endpoint, account, f"Clean {uuid.uuid4()}")
        resp = api_client.post(
            f"/api/messages/{target['id']}/action", json={"action": "not_spam"},
        )
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.click()
        toolbar = page.get_by_role("toolbar", name="Message actions")
        confirm = toolbar.get_by_role("button", name="Confirm this verdict", exact=True)
        expect(confirm).to_be_visible(timeout=15_000)

        confirm.click()
        expect(page.get_by_text("Marked as not spam", exact=True)).to_be_visible(timeout=5_000)
        expect(page.get_by_role("button", name="Undo", exact=True)).to_have_count(0)
        expect(row).to_be_visible()
        expect(confirm).to_be_visible()
        page.wait_for_timeout(2_000)
        expect(row).to_be_visible()
        assert _folder_of(api_client, target["id"]) == account["inbox"]["id"]

    def test_undoing_a_ruling_moves_the_message_back_and_reverses_the_ruling(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """Moving a message back out of Junk is itself a not-spam ruling
        (spam/feedback.py), so undo takes back both the move and the ruling."""
        junk = wait_for_folder(api_client, account["id"], "Junk")
        target = _deliver(api_client, dovecot_endpoint, account, f"Junked {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()
        row.get_by_title("Move to Junk").click()
        expect(row).not_to_be_visible(timeout=1_000)
        _wait_through_page(page, api_client, target["id"], junk["id"], "filed in Junk", 20.0)

        page.locator("body").click(position={"x": 1, "y": 1})
        page.keyboard.press("Control+z")
        expect(page.get_by_text("Undone: Marked as spam", exact=True)).to_be_visible(timeout=5_000)
        _wait_through_page(
            page, api_client, target["id"], account["inbox"]["id"], "moved back", 20.0,
        )
        wait_for(
            lambda: True
            if api_client.get(f"/api/mails/{target['id']}/verdict").json()["is_spam"] is False
            else None,
            timeout_s=20.0, description="the spam ruling reversed by the move back",
        )


class TestNothingIsPulledBack:
    def test_retry_sends_a_refused_action_again(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
    ) -> None:
        """Refused for want of an Archive folder; once there is one, Retry
        sends the same action and it lands."""
        bare = create_account(api_client, "intents-retry")
        bare["inbox"] = wait_for_folder(api_client, bare["id"], "INBOX")
        target = _deliver(api_client, dovecot_endpoint, bare, f"Retried {uuid.uuid4()}")
        _open_inbox(page, app_server, bare)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()
        row.get_by_title("Archive").click()
        chip = row.locator('[data-slot="row-action-failed"]')
        expect(chip).to_be_visible(timeout=10_000)

        archive = _create_folder(api_client, bare["id"], f"Archive-{uuid.uuid4().hex[:6]}")
        resp = api_client.patch(
            f"/api/folders/{archive['id']}/prefs", json={"special_use_override": "archive"},
        )
        assert resp.status_code == 200, resp.text

        chip.get_by_role("button", name="Retry", exact=True).click()
        expect(row).not_to_be_visible(timeout=10_000)
        _wait_through_page(page, api_client, target["id"], archive["id"], "archived on retry", 20.0)
        expect(page.get_by_test_id("actions-failed")).to_have_count(0)

    def test_a_late_archive_leaves_a_message_filed_elsewhere_meanwhile(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """Queued offline; filed into another folder from elsewhere before the
        connection returned. Sent late, the archive must not pull it out."""
        target = _deliver(api_client, dovecot_endpoint, account, f"Filed meanwhile {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        page.context.set_offline(True)
        row.hover()
        row.get_by_title("Archive").click()
        expect(row).not_to_be_visible(timeout=1_000)
        _move_elsewhere(api_client, target["id"], account["elsewhere"]["id"])

        page.context.set_offline(False)
        expect(
            page.get_by_text("Not archived — the message had already moved", exact=True)
        ).to_be_visible(timeout=30_000)
        assert _folder_of(api_client, target["id"]) == account["elsewhere"]["id"]

    def test_discarding_an_old_action_whose_answer_was_lost_moves_the_message_back(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """The archive reached the server and its answer never came back; the
        connection stayed dead for over an hour. Discarding it has to take
        back what may have happened rather than forget it."""
        target = _deliver(api_client, dovecot_endpoint, account, f"Answer lost {uuid.uuid4()}")
        page.clock.install()
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)

        sent: list[dict[str, Any]] = []

        def _lose_answer(route: Route, request: Request) -> None:
            sent.append(json.loads(request.post_data or "{}"))
            if len(sent) == 1:
                route.fetch()
            route.abort("internetdisconnected")

        page.route(_ACTION_ROUTE, _lose_answer)
        row.hover()
        row.get_by_title("Archive").click()
        _wait_through_page(
            page, api_client, target["id"], account["archive"]["id"], "archived", 20.0,
        )
        page.clock.fast_forward("01:05:00")
        held = page.get_by_test_id("actions-held")
        expect(held).to_contain_text("may have been sent", timeout=15_000)

        def _record(route: Route, request: Request) -> None:
            sent.append(json.loads(request.post_data or "{}"))
            route.continue_()

        page.unroute(_ACTION_ROUTE, _lose_answer)
        page.route(_ACTION_ROUTE, _record)
        held.get_by_role("button", name="Discard", exact=True).click()
        _wait_through_page(
            page, api_client, target["id"], account["inbox"]["id"], "moved back", 30.0,
        )
        keys = {body["idempotency_key"] for body in sent if body.get("action") == "archive"}
        assert len(keys) == 1, sent
        expect(held).to_have_count(0, timeout=10_000)

    def test_undo_leaves_a_message_filed_elsewhere_since(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        target = _deliver(api_client, dovecot_endpoint, account, f"Undo moved on {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        row = mail_row(page, target["id"])
        expect(row).to_be_visible(timeout=15_000)
        row.hover()
        row.get_by_title("Archive").click()
        _wait_through_page(
            page, api_client, target["id"], account["archive"]["id"], "archived", 20.0,
        )
        _move_elsewhere(api_client, target["id"], account["elsewhere"]["id"])

        page.locator("body").click(position={"x": 1, "y": 1})
        page.keyboard.press("Control+z")
        expect(
            page.get_by_text("Nothing to undo — the message has moved since", exact=True)
        ).to_be_visible(timeout=20_000)
        assert _folder_of(api_client, target["id"]) == account["elsewhere"]["id"]

    def test_undo_in_a_tab_that_does_not_send_still_moves_the_message_back(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        dovecot_endpoint: tuple[str, int, int],
        account: dict[str, Any],
    ) -> None:
        """Two tabs: the first opened sends for both. An archive and its undo
        taken in the second must both reach the server through the first."""
        target = _deliver(api_client, dovecot_endpoint, account, f"Second tab {uuid.uuid4()}")
        _open_inbox(page, app_server, account)
        second = page.context.new_page()
        try:
            _open_inbox(second, app_server, account)
            row = mail_row(second, target["id"])
            expect(row).to_be_visible(timeout=15_000)
            row.hover()
            row.get_by_title("Archive").click()
            _wait_through_page(
                second, api_client, target["id"], account["archive"]["id"], "archived", 20.0,
            )

            second.locator("body").click(position={"x": 1, "y": 1})
            second.keyboard.press("Control+z")
            expect(mail_row(second, target["id"])).to_be_visible(timeout=5_000)
            _wait_through_page(
                second, api_client, target["id"], account["inbox"]["id"], "moved back", 20.0,
            )
        finally:
            second.close()
