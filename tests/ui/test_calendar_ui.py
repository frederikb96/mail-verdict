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
    unique_email,
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
    wait_for_event_synced,
    wait_for_mailpit_message,
)
from tests.setup.dav_helpers import create_calendar, discover
from tests.ui.helpers import event_chip, wait_for_account_active

from tests.setup.containers import (  # isort: skip
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
)


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


@pytest.fixture(scope="module")
def organizer_identity(
    api_client: httpx.Client,
    dovecot_endpoint: tuple[str, int, int],
    calendar_collection: dict[str, Any],
) -> dict[str, Any]:
    """A real mail identity, linked to the shared calendar as its
    organiser -- an event created there with attendees is organised under
    this identity's own address, and deleting or editing it goes out over
    this identity's outbox."""
    _host, _imap_port, _lmtp_port = dovecot_endpoint
    email = unique_email("organizer")

    resp = api_client.post(
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
    wait_for_account_active(api_client, account["id"])

    resp = api_client.post("/api/identities", json={"account_id": account["id"], "address": email})
    assert resp.status_code == 201, resp.text
    identity = resp.json()

    resp = api_client.patch(
        f"/api/calendars/{calendar_collection['id']}", json={"identity_id": identity["id"]},
    )
    assert resp.status_code == 200, resp.text

    identity["email"] = email
    return identity


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

    def test_deleting_an_organised_event_with_guests_names_the_guest_count(
        self,
        page: Page,
        app_server: str,
        api_client: httpx.Client,
        mailpit_http_url: str,
        calendar_collection: dict[str, Any],
        organizer_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: "am I the organiser" was implemented
        as "the event has no organiser", true only for a purely local
        event -- so this event, organised by the calendar's own linked
        identity, showed the generic "cannot be undone" warning while
        still silently mailing a cancellation to its guest. The dialog
        should name the guest count instead."""
        summary = f"Organised {uuid.uuid4()}"
        resp = api_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": summary,
                "dtstart": "2026-09-16T10:00:00Z",
                "dtend": "2026-09-16T11:00:00Z",
                "attendees": [{"email": "guest@example.com"}],
            },
        )
        assert resp.status_code == 201, resp.text
        created = wait_for_event_synced(api_client, resp.json()["object_id"])
        assert created["organizer"] is not None
        assert created["organizer"]["email"].lower() == organizer_identity["email"].lower()

        page.goto(f"{app_server}/calendar")
        page.get_by_role("tab", name="Agenda", exact=True).click()
        chip = event_chip(page, created["object_id"])
        expect(chip).to_be_visible(timeout=15_000)
        chip.click()

        page.get_by_role("button", name="Delete", exact=True).click()
        expect(
            page.get_by_text(
                "A cancellation will be sent to 1 guest. This cannot be undone.", exact=True,
            )
        ).to_be_visible(timeout=10_000)
        page.get_by_role("button", name="Delete permanently", exact=True).click()

        expect(chip).to_be_hidden(timeout=10_000)
        wait_for_mailpit_message(mailpit_http_url, f"Cancelled: {summary}")

    def test_manage_calendars_selects_show_labels_not_raw_ids(
        self,
        page: Page,
        app_server: str,
        dav_account: dict[str, Any],
        calendar_collection: dict[str, Any],
        organizer_identity: dict[str, Any],
    ) -> None:
        """The regression this guards: every Select in this dialog rendered
        its raw stored value -- a UUID for the identity and the server, the
        internal enum member for the invitations intake -- instead of a
        label a person can read."""
        page.goto(f"{app_server}/calendar")
        page.get_by_role("button", name="Manage calendars", exact=True).click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=15_000)
        expect(dialog.get_by_text(organizer_identity["email"], exact=True)).to_be_visible(
            timeout=10_000
        )
        expect(dialog.get_by_text("Do nothing with invitations", exact=True)).to_be_visible(
            timeout=10_000
        )
        expect(dialog.get_by_text(dav_account["name"], exact=True)).to_be_visible(timeout=10_000)

        expect(dialog.get_by_text(calendar_collection["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(organizer_identity["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text(dav_account["id"], exact=False)).to_have_count(0)
        expect(dialog.get_by_text("none", exact=True)).to_have_count(0)

    def test_today_after_navigating_away_agrees_across_toolbar_grid_and_mini_month(
        self, page: Page, app_server: str,
    ) -> None:
        """The regression this guards: the month scroller's own scroll
        listener wrote the Monday of whatever week was passing under the
        top of the viewport into calendarDateAtom while Today's
        programmatic smooth-scroll was still animating, so Today could
        land on a different week's Monday instead of today's own weekday
        -- and the toolbar and the grid's own header, each computing the
        current month separately, could disagree about which month that
        even was. The mini-month, seeded once from the anchor, never
        followed navigation at all."""
        page.goto(f"{app_server}/calendar")

        page.get_by_role("tab", name="Day", exact=True).click()
        today_title = page.get_by_test_id("calendar-toolbar-title").text_content()
        assert today_title

        page.get_by_role("tab", name="Month", exact=True).click()
        initial_month = page.get_by_test_id("calendar-toolbar-title").text_content()
        assert initial_month

        for _ in range(3):
            page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_role("button", name="Today", exact=True).click()

        toolbar_title = page.get_by_test_id("calendar-toolbar-title")
        expect(toolbar_title).to_have_text(initial_month, timeout=10_000)
        expect(page.get_by_test_id("month-grid-title")).to_have_text(initial_month, timeout=10_000)
        expect(page.get_by_test_id("mini-month-title")).to_have_text(initial_month, timeout=10_000)

        page.get_by_role("tab", name="Day", exact=True).click()
        expect(toolbar_title).to_have_text(today_title, timeout=10_000)
