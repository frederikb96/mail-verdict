"""
Calendar UI actions against a real Radicale server -- the event editor's own
edit path, which the API and e2e layers do not exercise since neither drives
the actual form. Every action here goes through a control a person clicks,
and the assertion that an edit landed reads the real server back through
api_client, never a mock.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import (
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
    wait_for_event_synced,
)
from tests.setup.dav_helpers import create_calendar, discover
from tests.ui.helpers import event_chip

from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT  # isort: skip


@pytest.fixture(scope="module")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="module")
def ui_calendar_owner(radicale_base_url: str) -> str:
    """A calendar on the real Radicale server, owned by a fresh principal --
    the dav_account below points at it. Returns the username."""
    username = f"cal-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        create_calendar(client, principal, "work", "Work")
    return username


@pytest.fixture(scope="module")
def dav_account(api_client: httpx.Client, ui_calendar_owner: str) -> dict[str, Any]:
    resp = api_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": ui_calendar_owner,
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(api_client, account["id"])
    return account


@pytest.fixture(scope="module")
def calendar_collection(api_client: httpx.Client, dav_account: dict[str, Any]) -> dict[str, Any]:
    return wait_for_dav_collection(api_client, dav_account["id"], "Work")


class TestCalendarUi:
    """Shares one DAV account and its one synced calendar."""

    def test_renaming_an_event_from_the_editor_succeeds(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor always sent an attendees
        array on every update, and the backend refuses any attendees value
        outright -- so this rename returned 422 before the editor stopped
        sending the field it cannot change anyway. Named to avoid the word
        "edit" in the event summary, which would otherwise collide with the
        Edit button's own accessible name under substring matching."""
        original_summary = f"Standup {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": original_summary,
                "dtstart": "2026-09-15T10:00:00Z",
                "dtend": "2026-09-15T11:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])

        new_summary = f"Renamed {uuid.uuid4()}"

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda").click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        page.get_by_role("button", name="Edit", exact=True).click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(new_summary)
        page.get_by_role("button", name="Save", exact=True).click()

        expect(page.get_by_text("Event updated")).to_be_visible(timeout=10_000)

        def _renamed() -> dict[str, Any] | None:
            detail = api_client.get(f"/api/calendar/events/{created['object_id']}").json()
            return detail if detail["summary"] == new_summary else None

        wait_for(_renamed, description="Renamed event synced back")

    def test_creating_a_timed_event_binds_the_browsers_own_timezone(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        calendar_collection: dict[str, Any],
    ) -> None:
        """The regression this guards: the editor never sent `tz` on
        create, so a timed event lost its named zone regardless of what
        the API itself could do with one -- the API's own binding
        behaviour for a given zone (kept wall-clock reading, moved
        instant) is proven directly in
        TestCreateWithTimezone in the pg layer; this only has to show the
        editor actually sends the field at all. Pinning the browser to a
        specific zone via `browser_context_args`/the marker was tried and
        dropped: this app's SSR renders with the server's own (host) zone,
        so a client pinned to a different one hydrates mismatched
        (React error #418) and the toolbar's click handlers go missing --
        a real bug, but not one this row owns. Reading the browser's own
        zone back keeps the assertion meaningful without touching it."""
        summary = f"DST check {uuid.uuid4()}"

        page.goto(f"{app_server}/calendar")
        browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        # The editor seeds its Calendar field from useCalendars() at the
        # moment it mounts and never re-reads it once that query resolves
        # later -- opening it before the sidebar's own calendar list has
        # loaded leaves Save permanently disabled.
        expect(page.get_by_role("checkbox", name="Work")).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="New event").click()
        title_input = page.get_by_label("Title")
        expect(title_input).to_be_visible(timeout=15_000)
        title_input.fill(summary)

        # fill() on a datetime-local input sets the DOM value without
        # React ever seeing it -- go through the input's own native
        # setter and fire the event React listens for instead.
        starts_input, ends_input = page.locator('input[type="datetime-local"]').all()
        for locator, value in (
            (starts_input, "2026-09-10T10:00"), (ends_input, "2026-09-10T11:00"),
        ):
            locator.evaluate(
                "(el, value) => {"
                "  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')"
                "    .set.call(el, value);"
                "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                "}",
                value,
            )

        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Event created")).to_be_visible(timeout=10_000)

        def _created() -> dict[str, Any] | None:
            listed = api_client.get("/api/calendar/events", params={"month": "2026-09"}).json()
            return next((e for e in listed["events"] if e["summary"] == summary), None)

        event = wait_for(_created, description="Created event synced back")
        assert event["tz"] == browser_tz
