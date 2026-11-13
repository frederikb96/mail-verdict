"""DAV accounts, calendars, events, invitations, address books and contacts."""

from __future__ import annotations

import uuid
from typing import Any

from starlette.testclient import TestClient

from tests.e2e.isolation import RouteCase, ToolCase, World
from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT
from tests.setup.dav_helpers import SAMPLE_EVENT_MONTH


def _dav_account(world: World) -> dict[str, str]:
    return {"dav_account_id": world.b_dav_account_id}


def _calendar(world: World) -> dict[str, str]:
    return {"collection_id": world.b["calendar_id"]}


def _event(world: World) -> dict[str, str]:
    return {"object_id": world.b["event_id"]}


def _contact(world: World) -> dict[str, str]:
    return {"contact_id": world.b["contact_id"]}


def _message(world: World) -> dict[str, str]:
    return {"message_id": world.b["message_id"]}


def _event_window(world: World) -> dict[str, str]:
    return {"month": SAMPLE_EVENT_MONTH, "calendars": world.b["calendar_id"]}


def _new_event(world: World) -> dict[str, Any]:
    return {
        "calendar_id": world.b["calendar_id"], "summary": "Planted by A",
        "dtstart": "2026-09-11T10:00:00+00:00", "dtend": "2026-09-11T11:00:00+00:00",
    }


def _own_dav_account(world: World) -> dict[str, Any]:
    return {
        "name": f"Second {uuid.uuid4().hex[:8]}",
        "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
        "username": f"iso-a-extra-{uuid.uuid4().hex[:8]}", "password": "unused",
    }


ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/dav-accounts", "own"),
    RouteCase("POST /api/dav-accounts", "own", body=_own_dav_account),
    RouteCase("GET /api/dav-accounts/{dav_account_id}", "account_path", params=_dav_account),
    RouteCase(
        "PATCH /api/dav-accounts/{dav_account_id}", "account_path",
        params=_dav_account, body=lambda world: {"is_active": False},
    ),
    RouteCase("DELETE /api/dav-accounts/{dav_account_id}", "account_path", params=_dav_account),
    RouteCase(
        "POST /api/dav-accounts/{dav_account_id}/sync", "account_path", params=_dav_account,
    ),
    RouteCase("GET /api/calendars", "own"),
    RouteCase(
        "POST /api/calendars", "resource_id",
        body=lambda world: {
            "dav_account_id": world.b_dav_account_id, "display_name": "Planted by A",
        },
    ),
    RouteCase(
        "PATCH /api/calendars/{collection_id}", "resource_id",
        params=_calendar, body=lambda world: {"is_visible": False},
    ),
    RouteCase("DELETE /api/calendars/{collection_id}", "resource_id", params=_calendar),
    RouteCase("GET /api/calendar/links", "own"),
    RouteCase(
        "PUT /api/calendar/links", "own", body=lambda world: {"base_revision": 0, "rows": []},
    ),
    RouteCase("GET /api/addressbooks", "own"),
    RouteCase(
        "POST /api/addressbooks", "resource_id",
        body=lambda world: {
            "dav_account_id": world.b_dav_account_id, "display_name": "Planted by A",
        },
    ),
    RouteCase("GET /api/calendar/events", "own", query=_event_window),
    RouteCase("POST /api/calendar/events", "resource_id", body=_new_event),
    RouteCase("GET /api/calendar/events/{object_id}", "resource_id", params=_event),
    RouteCase(
        "PATCH /api/calendar/events/{object_id}", "resource_id",
        params=_event, body=lambda world: {"summary": "Rewritten by A"},
    ),
    RouteCase("DELETE /api/calendar/events/{object_id}", "resource_id", params=_event),
    RouteCase(
        "POST /api/calendar/events/{object_id}/respond", "resource_id",
        params=_event,
        body=lambda world: {"identity_id": world.b["identity_id"], "partstat": "accepted"},
    ),
    RouteCase(
        "GET /api/calendar/invitations/{message_id}", "resource_id", params=_message,
    ),
    RouteCase(
        "POST /api/calendar/invitations/{message_id}/import", "resource_id",
        params=_message, body=lambda world: {"calendar_id": world.b["calendar_id"]},
    ),
    RouteCase("GET /api/contacts", "own"),
    RouteCase(
        "POST /api/contacts", "resource_id",
        body=lambda world: {
            "addressbook_id": world.b["addressbook_id"], "summary": "Planted by A",
            "emails": [{"email": "planted@example.com"}],
        },
    ),
    RouteCase("GET /api/contacts/groups", "own"),
    RouteCase("GET /api/contacts/photo-index", "own"),
    RouteCase(
        "GET /api/contacts/resolve", "own", query=lambda world: {"email": world.b["contact_email"]},
    ),
    RouteCase(
        "GET /api/contacts/search", "own", query=lambda world: {"q": world.b["contact_email"]},
    ),
    RouteCase("GET /api/contacts/{contact_id}", "resource_id", params=_contact),
    RouteCase(
        "PATCH /api/contacts/{contact_id}", "resource_id",
        params=_contact, body=lambda world: {"summary": "Rewritten by A"},
    ),
    RouteCase("DELETE /api/contacts/{contact_id}", "resource_id", params=_contact),
    RouteCase("GET /api/contacts/{contact_id}/photo", "resource_id", params=_contact),
]


TOOL_CASES: list[ToolCase] = [
    ToolCase("list_calendars", "own"),
    ToolCase(
        "list_events", "own",
        arguments=lambda world: {
            "month": SAMPLE_EVENT_MONTH, "calendar_ids": world.b["calendar_id"],
        },
    ),
    ToolCase("list_addressbooks", "own"),
    ToolCase("list_contacts", "own"),
    ToolCase(
        "search_contacts", "own", arguments=lambda world: {"q": world.b["contact_email"]},
    ),
    ToolCase(
        "get_event", "resource_id", arguments=lambda world: {"event_id": world.b["event_id"]},
    ),
    ToolCase(
        "create_event", "resource_id",
        arguments=lambda world: {
            "calendar_id": world.b["calendar_id"], "summary": "Planted by A",
            "dtstart": "2026-09-11T10:00:00+00:00", "dtend": "2026-09-11T11:00:00+00:00",
        },
    ),
    ToolCase(
        "update_event", "resource_id",
        arguments=lambda world: {"event_id": world.b["event_id"], "summary": "Rewritten by A"},
    ),
    ToolCase(
        "delete_event", "resource_id", arguments=lambda world: {"event_id": world.b["event_id"]},
    ),
    ToolCase(
        "respond_to_event", "resource_id",
        arguments=lambda world: {
            "event_id": world.b["event_id"], "identity_id": world.b["identity_id"],
            "partstat": "accepted",
        },
    ),
    ToolCase(
        "get_contact", "resource_id",
        arguments=lambda world: {"contact_id": world.b["contact_id"]},
    ),
    ToolCase(
        "create_contact", "resource_id",
        arguments=lambda world: {
            "addressbook_id": world.b["addressbook_id"], "summary": "Planted by A",
            "emails": [{"email": "planted@example.com"}],
        },
    ),
    ToolCase(
        "update_contact", "resource_id",
        arguments=lambda world: {"contact_id": world.b["contact_id"], "summary": "Rewritten by A"},
    ),
    ToolCase(
        "delete_contact", "resource_id",
        arguments=lambda world: {"contact_id": world.b["contact_id"]},
    ),
]


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _links(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get("/api/calendar/links", headers=_as(token))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def check_links_belong_to_their_owner(client: TestClient, world: World) -> None:
    """A user's links document touches only that user's links: B's survives
    A replacing their own (empty) document, and A cannot name B's calendar or
    identity in theirs."""
    linked = client.put(
        "/api/calendar/links", headers=_as(world.b_token),
        json={
            "base_revision": _links(client, world.b_token)["base_revision"],
            "rows": [{
                "identity_id": world.b["identity_id"],
                "calendar_ids": [world.b["calendar_id"]],
                "receives_invitations_calendar_id": world.b["calendar_id"],
            }],
        },
    )
    assert linked.status_code == 200, linked.text

    replaced = client.put(
        "/api/calendar/links", headers=_as(world.a_token),
        json={"base_revision": _links(client, world.a_token)["base_revision"], "rows": []},
    )
    assert replaced.status_code == 200, replaced.text
    survivors = _links(client, world.b_token)["rows"]
    assert [row["calendar_ids"] for row in survivors] == [[world.b["calendar_id"]]]

    for identity_id, calendar_id in (
        (world.b["identity_id"], world.b["calendar_id"]),
        (world.b["identity_id"], None),
        (None, world.b["calendar_id"]),
    ):
        stolen = client.put(
            "/api/calendar/links", headers=_as(world.a_token),
            json={
                "base_revision": _links(client, world.a_token)["base_revision"],
                "rows": [{
                    "identity_id": identity_id or str(uuid.uuid4()),
                    "calendar_ids": [calendar_id] if calendar_id else [],
                    "receives_invitations_calendar_id": calendar_id,
                }],
            },
        )
        assert stolen.status_code in (403, 404), stolen.text


def check_event_lists_never_reach_another_users_calendars(
    client: TestClient, world: World,
) -> None:
    """Naming B's calendar in the month query returns nothing of B's, and
    B still sees their own event afterwards."""
    listed = client.get(
        "/api/calendar/events", headers=_as(world.a_token),
        params={"month": SAMPLE_EVENT_MONTH, "calendars": world.b["calendar_id"]},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["events"] == []

    own = client.get(
        "/api/calendar/events", headers=_as(world.b_token),
        params={"month": SAMPLE_EVENT_MONTH, "calendars": world.b["calendar_id"]},
    )
    assert [e["object_id"] for e in own.json()["events"]] == [world.b["event_id"]]


def check_contact_lookups_stay_inside_the_callers_books(
    client: TestClient, world: World,
) -> None:
    """Listing, searching and resolving by B's address book or address finds
    nothing for A, and B's contact is still there."""
    listed = client.get(
        "/api/contacts", headers=_as(world.a_token),
        params={"addressbook_id": world.b["addressbook_id"]},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["contacts"] == []
    resolved = client.get(
        "/api/contacts/resolve", headers=_as(world.a_token),
        params={"email": world.b["contact_email"]},
    )
    assert resolved.status_code == 200 and resolved.json() is None
    hits = client.get(
        "/api/contacts/search", headers=_as(world.a_token), params={"q": "b-contact"},
    )
    assert hits.status_code == 200 and hits.json() == []

    own = client.get(
        "/api/contacts", headers=_as(world.b_token),
        params={"addressbook_id": world.b["addressbook_id"]},
    )
    assert [c["id"] for c in own.json()["contacts"]] == [world.b["contact_id"]]


def check_the_photo_index_refuses_a_foreign_account(client: TestClient, world: World) -> None:
    refused = client.get(
        "/api/contacts/photo-index", headers=_as(world.a_token),
        params={"account_id": world.b_account_id},
    )
    assert refused.status_code == 403, refused.text


CHECKS = [
    check_links_belong_to_their_owner,
    check_event_lists_never_reach_another_users_calendars,
    check_contact_lookups_stay_inside_the_callers_books,
    check_the_photo_index_refuses_a_foreign_account,
]
