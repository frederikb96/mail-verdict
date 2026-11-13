"""Alerts, the badge, push devices and the live event stream: every alert and
device is one user's, and a stream -- live or replayed from a Last-Event-ID --
carries only its caller's events."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.testclient import TestClient

from mail_verdict.api.events import get_event_ring
from tests.e2e.isolation import RouteCase, World
from tests.setup.auth import bearer


def _b_alert(world: World) -> dict[str, str]:
    return {"alert_id": world.b["alert_id"]}


def _b_subscription(world: World) -> dict[str, str]:
    return {"subscription_id": world.b["subscription_id"]}


def _browser(world: World) -> dict[str, Any]:
    return {
        "endpoint": f"https://push.example/a-{uuid.uuid4()}", "keys": {"p256dh": "k", "auth": "a"},
    }


def _phone(world: World) -> dict[str, Any]:
    return {
        "installation_id": str(uuid.uuid4()), "relay_url": "https://push.frederikberg.net",
        "ticket": "isolation-ticket", "content_key": "A" * 43 + "=",
    }


ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/alerts", "own", query=lambda world: {"limit": "200"}),
    RouteCase("GET /api/alerts/unseen-count", "own"),
    RouteCase("GET /api/alerts/badge", "own"),
    # Dismissing someone else's alert is a no-op, like dismissing one twice;
    # a_dismissal_leaves_bs_alert_alone checks B's alert is untouched.
    RouteCase("POST /api/alerts/{alert_id}/dismiss", "own", params=_b_alert),
    RouteCase("POST /api/alerts/dismiss-all", "own"),
    RouteCase(
        "POST /api/alerts/lookup", "own", body=lambda world: {"ids": [world.b["alert_id"]]},
    ),
    RouteCase("GET /api/alerts/vapid-public-key", "own"),
    RouteCase("GET /api/alerts/native-push", "own"),
    RouteCase("GET /api/alerts/subscriptions", "own"),
    RouteCase("POST /api/alerts/subscriptions", "own", body=_browser),
    # The test stack has no ENCRYPTION_KEY, so native push is unavailable.
    RouteCase("POST /api/alerts/subscriptions/native", "own", body=_phone, also=(503,)),
    RouteCase(
        "PATCH /api/alerts/subscriptions/{subscription_id}", "resource_id",
        params=_b_subscription, body=lambda world: {"label": "taken"},
    ),
    RouteCase(
        "DELETE /api/alerts/subscriptions/{subscription_id}", "resource_id",
        params=_b_subscription,
    ),
    RouteCase(
        "POST /api/alerts/subscriptions/{subscription_id}/test", "resource_id",
        params=_b_subscription,
    ),
    # the_stream_carries_none_of_bs_events below is what holds the stream.
    RouteCase("GET /api/events", "stream"),
]


def a_dismissal_leaves_bs_alert_alone(client: TestClient, world: World) -> None:
    """A dismissing B's alert, one by one or all at once, changes nothing of
    B's: B still sees it unseen, and A's own count never included it."""
    mine, theirs = bearer(world.a_token), bearer(world.b_token)
    client.post(f"/api/alerts/{world.b['alert_id']}/dismiss", headers=mine)
    client.post("/api/alerts/dismiss-all", headers=mine)

    unseen = client.get("/api/alerts", headers=theirs, params={"unseen_only": "true"})
    assert world.b["alert_id"] in {a["id"] for a in unseen.json()}, unseen.text
    assert client.get("/api/alerts/unseen-count", headers=theirs).json()["unseen"] >= 1
    assert client.get("/api/alerts/unseen-count", headers=mine).json()["unseen"] == 0


def bs_device_is_not_as_badge(client: TestClient, world: World) -> None:
    """A badge asked for one of B's devices is refused, not computed."""
    response = client.get(
        "/api/alerts/badge", headers=bearer(world.a_token),
        params={"subscription_id": world.b["subscription_id"]},
    )
    assert response.status_code == 403, response.text


_EVENT_ID = re.compile(r"^id: (\S+)$", re.MULTILINE)


async def _stream(
    app: Any, token: str, during: Callable[[Callable[[str], Awaitable[None]]], Awaitable[None]],
    *, query: str = "", last_event_id: str | None = None,
) -> str:
    """Hold GET /api/events open as `token` through the whole application,
    run `during` -- which may wait for text to arrive -- and return all the
    stream carried by then."""
    headers = [(b"host", b"testserver"), (b"authorization", f"Bearer {token}".encode())]
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/events", "raw_path": b"/api/events",
        "query_string": query.encode(), "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    received: list[str] = []
    arrived = asyncio.Event()
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 200, message
        elif message["type"] == "http.response.body":
            received.append(message.get("body", b"").decode())
            arrived.set()

    async def wait_for(text: str) -> None:
        async with asyncio.timeout(15):
            while text not in "".join(received):
                arrived.clear()
                await arrived.wait()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await during(wait_for)
    finally:
        disconnected.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return "".join(received)


def the_stream_carries_none_of_bs_events(client: TestClient, world: World) -> None:
    """A's stream, live and replayed from its Last-Event-ID, and narrowed to
    B's account by query, never carries an event of B's account or of B
    the user -- while A's own events, sent alongside, do arrive."""
    ring = get_event_ring()
    assert ring is not None
    secret = f"b-only-{uuid.uuid4().hex}"
    mine = f"a-control-{uuid.uuid4().hex}"
    connected: dict[str, str] = {}

    async def live(wait_for: Callable[[str], Awaitable[None]]) -> None:
        await wait_for("event: connected")
        await ring.add(uuid.UUID(world.b_account_id), "mail.updated", {"secret": secret})
        await ring.add(uuid.UUID(world.b_user_id), "order.updated", {"secret": secret})
        await ring.add(uuid.UUID(world.b_dav_account_id), "calendar.object", {"secret": secret})
        await ring.add(uuid.UUID(world.a_account_id), "mail.updated", {"mine": mine})
        await wait_for(mine)

    async def scenario() -> tuple[str, str, str]:
        first = await _stream(client.app, world.a_token, live)
        connected["id"] = _EVENT_ID.findall(first.split("event: connected")[0])[-1]

        async def replay(wait_for: Callable[[str], Awaitable[None]]) -> None:
            await wait_for(mine)

        replayed = await _stream(
            client.app, world.a_token, replay, last_event_id=connected["id"],
        )

        async def narrowed_live(wait_for: Callable[[str], Awaitable[None]]) -> None:
            await wait_for("event: connected")
            await ring.add(uuid.UUID(world.b_account_id), "mail.updated", {"secret": secret})
            await ring.add(uuid.UUID(world.a_user_id), "order.updated", {"mine": mine})
            await wait_for(mine)

        narrowed = await _stream(
            client.app, world.a_token, narrowed_live, query=f"account_id={world.b_account_id}",
        )
        return first, replayed, narrowed

    first, replayed, narrowed = client.portal.call(scenario)

    for stream in (first, replayed, narrowed):
        assert mine in stream
        assert secret not in stream, stream[-2000:]
    assert "event: resync" not in replayed


CHECKS = [
    a_dismissal_leaves_bs_alert_alone,
    bs_device_is_not_as_badge,
    the_stream_carries_none_of_bs_events,
]
