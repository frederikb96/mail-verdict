"""
Calendar events against a real Radicale server, through a real PostIMAP -- the round
trip the project's own test notes flag as unproven: that a real sync fills
`dav_objects`' parsed columns the shape the query layer assumes, in both directions.

Events pre-existing on the server before the account exists are discovered on backfill
(the calendar-side counterpart of test_account_flow.py's onboarded_account); an event
created through the API is verified to have actually reached the server, independent of
MailVerdict's own read path; and an event added directly on the server is verified to
reach the API after a sync.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from tests.e2e.helpers import (
    wait_for,
    wait_for_dav_account_active,
    wait_for_dav_collection,
    wait_for_event_synced,
)
from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT
from tests.setup.dav_helpers import create_calendar, discover, get_object, put_object, sample_event

SEEDED_EVENTS = {
    f"seed-{i}@e2e.test.local": summary
    for i, summary in enumerate(["Team standup", "Quarterly planning", "Dentist appointment"])
}


@pytest.fixture(scope="class")
def radicale_base_url(radicale_endpoint: tuple[str, int]) -> str:
    host, port = radicale_endpoint
    return f"http://{host}:{port}/"


@pytest.fixture(scope="class")
def seeded_calendar(radicale_base_url: str) -> dict[str, object]:
    """A calendar pre-populated directly on the real server before any dav_account
    exists -- the realistic "point an account at an established calendar" case, the
    same role onboarded_account's pre-existing mailbox plays for mail."""
    username = f"cal-{uuid.uuid4().hex[:8]}"
    with httpx.Client(auth=(username, "unused"), timeout=10.0) as client:
        principal = discover(client, radicale_base_url)
        calendar_url = create_calendar(client, principal, "work", "Work")
        for uid, summary in SEEDED_EVENTS.items():
            put_object(
                client, f"{calendar_url}{uid}.ics", sample_event(uid, summary),
                "text/calendar; charset=utf-8",
            )
    return {"username": username, "calendar_url": calendar_url}


@pytest.fixture(scope="class")
def dav_account(app_client: TestClient, seeded_calendar: dict[str, object]) -> dict[str, object]:
    resp = app_client.post(
        "/api/dav-accounts",
        json={
            "name": f"Radicale-{uuid.uuid4().hex[:8]}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": seeded_calendar["username"],
            "password": "unused",
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()
    wait_for_dav_account_active(app_client, account["id"])
    return account


@pytest.fixture(scope="class")
def calendar_collection(
    app_client: TestClient, dav_account: dict[str, object],
) -> dict[str, object]:
    return wait_for_dav_collection(app_client, dav_account["id"], "Work")


class TestCalendarRoundTrip:
    def test_pre_existing_events_are_discovered_on_account_creation(
        self, calendar_collection: dict[str, object],
    ) -> None:
        assert calendar_collection["total_count"] == len(SEEDED_EVENTS)

    def test_events_seeded_directly_on_the_server_appear_through_the_api(
        self, app_client: TestClient, calendar_collection: dict[str, object],
    ) -> None:
        resp = app_client.get(
            "/api/calendar/events",
            params={"month": "2026-09", "calendars": calendar_collection["id"]},
        )
        assert resp.status_code == 200, resp.text
        summaries = {e["summary"] for e in resp.json()["events"]}
        assert summaries == set(SEEDED_EVENTS.values())

    def test_creating_an_event_reaches_the_real_server(
        self,
        app_client: TestClient,
        calendar_collection: dict[str, object],
        seeded_calendar: dict[str, object],
    ) -> None:
        resp = app_client.post(
            "/api/calendar/events",
            json={
                "calendar_id": calendar_collection["id"],
                "summary": "Created via API",
                "dtstart": "2026-09-15T10:00:00Z",
                "dtend": "2026-09-15T11:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["pending"] is True

        synced = wait_for_event_synced(app_client, created["object_id"])
        assert synced["uid"] == created["uid"]

        # Read the object straight off the real server, bypassing MailVerdict's own
        # read path entirely -- proves the PUT itself landed, not just this row's etag.
        with httpx.Client(auth=(seeded_calendar["username"], "unused"), timeout=10.0) as client:
            body = get_object(client, f"{seeded_calendar['calendar_url']}{synced['uid']}.ics")
        assert "SUMMARY:Created via API" in body

    def test_an_event_added_on_the_server_appears_through_the_api_after_sync(
        self,
        app_client: TestClient,
        dav_account: dict[str, object],
        calendar_collection: dict[str, object],
        seeded_calendar: dict[str, object],
    ) -> None:
        uid = f"server-side-{uuid.uuid4().hex[:8]}@e2e.test.local"
        with httpx.Client(auth=(seeded_calendar["username"], "unused"), timeout=10.0) as client:
            put_object(
                client, f"{seeded_calendar['calendar_url']}{uid}.ics",
                sample_event(uid, "Added on the server"), "text/calendar; charset=utf-8",
            )

        sync_resp = app_client.post(f"/api/dav-accounts/{dav_account['id']}/sync")
        assert sync_resp.status_code == 200, sync_resp.text

        def _check() -> dict[str, object] | None:
            resp = app_client.get(
                "/api/calendar/events",
                params={"month": "2026-09", "calendars": calendar_collection["id"]},
            )
            assert resp.status_code == 200, resp.text
            return next(
                (e for e in resp.json()["events"] if e["summary"] == "Added on the server"), None,
            )

        found = wait_for(
            _check, timeout_s=30.0, description="Server-side event synced into MailVerdict",
        )
        assert found["uid"] == uid
