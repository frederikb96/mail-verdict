"""
Two users on one instance, and every route and MCP tool checked against them.

The first tests need no containers: they enumerate what the application
serves and fail on anything nobody has classified (see
tests/e2e/isolation/__init__.py for the kinds and pending.py for what is
still unclassified). The rest build a World -- users A and B, each with a
real mail account and a CalDAV account, B with a delivered message, a
calendar event and a row of everything else a user owns -- and check every
classified route and tool against it as A.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from mail_verdict.auth.middleware import PUBLIC_PATHS
from tests.e2e.helpers import unique_email
from tests.e2e.isolation import (
    UI_ROUTES,
    RouteCase,
    World,
    checks,
    enumerate_routes,
    enumerate_tools,
    fill_path,
    route_cases,
    tool_cases,
)
from tests.e2e.isolation.pending import PENDING_ROUTES, PENDING_TOOLS
from tests.helpers.mcp_http import McpHttpSession
from tests.setup.auth import OWNER_SUBJECT, TestIssuer, bearer
from tests.setup.containers import (
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_PASSWORD,
    RADICALE_ALIAS,
    RADICALE_PORT,
)
from tests.setup.dav_helpers import (
    SAMPLE_EVENT_MONTH,
    create_addressbook,
    create_calendar,
    discover,
    put_object,
    sample_contact,
    sample_event,
)
from tests.setup.mail_delivery import build_eml, deliver_message

# A 1x1 PNG, base64: B's contact photo.
_PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5E"
    "rkJggg=="
)

# Sent as the Authorization header to make a request carry no token at all,
# overriding the owner's token app_client sends by default.
_NO_TOKEN = {"Authorization": ""}


@pytest.fixture()
def served_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The composed app, built without starting anything -- enough to
    enumerate its routes."""
    from mail_verdict.config.loader import reset_config

    monkeypatch.setenv("MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    reset_config()
    from mail_verdict.server import create_app

    app = create_app()
    reset_config()
    return app


def _classified_routes() -> list[str]:
    return [case.route for case in route_cases()]


class TestEverythingIsClassified:
    """Nothing the application serves escapes the harness."""

    def test_every_route_is_classified_exactly_once(self, served_app: Any) -> None:
        served = enumerate_routes(served_app) - UI_ROUTES
        cases = _classified_routes()
        duplicates = sorted(
            {r for r in cases if cases.count(r) > 1} | (set(cases) & PENDING_ROUTES)
        )
        classified = set(cases) | PENDING_ROUTES

        assert not duplicates, f"classified twice: {duplicates}"
        assert not sorted(served - classified), (
            "served but never classified -- add a case for each in an area module of "
            f"tests/e2e/isolation/: {sorted(served - classified)}"
        )
        assert not sorted(classified - served), (
            f"classified but no longer served: {sorted(classified - served)}"
        )

    def test_every_mcp_tool_is_classified_exactly_once(self) -> None:
        served = asyncio.run(enumerate_tools())
        cases = [case.tool for case in tool_cases()]
        duplicates = sorted({t for t in cases if cases.count(t) > 1} | (set(cases) & PENDING_TOOLS))
        classified = set(cases) | PENDING_TOOLS

        assert not duplicates, f"classified twice: {duplicates}"
        assert not sorted(served - classified), (
            f"tools never classified: {sorted(served - classified)}"
        )
        assert not sorted(classified - served), (
            f"classified tools that no longer exist: {sorted(classified - served)}"
        )

    def test_only_the_web_ui_is_served_outside_api_and_mcp(self, served_app: Any) -> None:
        outside = {
            route for route in enumerate_routes(served_app)
            if not route.split(" ", 1)[1].startswith(("/api", "/mcp"))
        }

        assert outside <= UI_ROUTES

    def test_the_public_cases_are_exactly_what_the_gate_lets_through(self) -> None:
        public = {
            case.route.split(" ", 1)[1] for case in route_cases() if case.kind == "public"
        }

        assert public == PUBLIC_PATHS


def _poll(check: Callable[[], Any], what: str, timeout_s: float = 90.0) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(1)
    raise TimeoutError(f"{what} did not happen within {timeout_s}s (last: {last!r})")


def _as(token: str) -> dict[str, str]:
    return bearer(token)


def _add_mail_account(client: TestClient, token: str, email: str) -> str:
    response = client.post(
        "/api/accounts", headers=_as(token),
        json={
            "name": email, "imap_host": DOVECOT_ALIAS, "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email, "imap_password": DOVECOT_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    account_id: str = response.json()["id"]
    return account_id


def _add_dav_account(client: TestClient, token: str, username: str) -> str:
    response = client.post(
        "/api/dav-accounts", headers=_as(token),
        json={
            "name": f"Radicale {username}",
            "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
            "username": username, "password": "unused",
        },
    )
    assert response.status_code == 201, response.text
    dav_account_id: str = response.json()["id"]
    return dav_account_id


async def _seed_owned_rows(postgres_url: str, rows: dict[str, Any]) -> None:
    """B's rows in the tables a user owns without an account key, written
    directly: the routes that create them do not stamp an owner yet."""
    engine = create_async_engine(postgres_url)
    b = rows["b_user_id"]
    try:
        async with engine.begin() as conn:
            document = await conn.scalar(
                text("SELECT document FROM pipeline_revisions ORDER BY revision DESC LIMIT 1")
            )
            # B's own document, starting as a copy of the newest revision.
            rows["pipeline_revision"] = str(await conn.scalar(
                text(
                    "INSERT INTO pipeline_revisions (document, note, user_id) "
                    "VALUES (CAST(:d AS jsonb), 'isolation', :b) RETURNING revision"
                ),
                {"d": json.dumps(document or {}), "b": b},
            ))
            await conn.execute(
                text("INSERT INTO unified_views (id, name, user_id) VALUES (:id, :name, :b)"),
                {"id": rows["view_id"], "name": f"B view {rows['tag']}", "b": b},
            )
            await conn.execute(
                text("INSERT INTO unified_view_folders (view_id, folder_id) VALUES (:v, :f)"),
                {"v": rows["view_id"], "f": rows["folder_id"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, kind, deliver_at, delivered_at, dedupe_key, "
                    "account_id, message_id, folder_id, title, user_id) VALUES (:id, 'mail', "
                    "now(), now(), :key, :account, :message, :folder, :title, :b)"
                ),
                {
                    "id": rows["alert_id"], "key": f"isolation:{rows['alert_id']}",
                    "account": rows["account_id"], "message": rows["message_id"],
                    "folder": rows["folder_id"], "title": rows["subject"], "b": b,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO orders (id, merchant, subject, written_at, user_id) "
                    "VALUES (:id, :merchant, :subject, now(), :b)"
                ),
                {
                    "id": rows["order_id"], "merchant": f"Merchant {rows['tag']}",
                    "subject": rows["subject"], "b": b,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO push_subscriptions (id, transport, endpoint, p256dh, auth, "
                    "user_id) VALUES (:id, 'webpush', :endpoint, 'k', 'a', :b)"
                ),
                {
                    "id": rows["subscription_id"],
                    "endpoint": f"https://push.example/{rows['tag']}", "b": b,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO identities (id, account_id, email, is_default) "
                    "VALUES (:id, :account, :email, true)"
                ),
                {"id": rows["identity_id"], "account": rows["account_id"], "email": rows["email"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO verdicts (id, mail_id, account_id, msg_key, from_addr, "
                    "is_spam, model_used, reasoning, source) VALUES (:id, :message, :account, "
                    ":key, :sender, true, 'isolation-model', :reasoning, 'ai')"
                ),
                {
                    "id": rows["verdict_id"], "message": rows["message_id"],
                    "account": rows["account_id"], "key": f"<isolation-{rows['tag']}@example.com>",
                    "sender": rows["sender"], "reasoning": f"Reasoning {rows['tag']}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_runs (id, account_id, msg_key, message_id, dedup_key, "
                    "origin, status, last_error) VALUES (:id, :account, :key, :message, :key, "
                    "'live', 'failed', :error)"
                ),
                {
                    "id": rows["run_id"], "account": rows["account_id"],
                    "key": f"isolation-run-{rows['tag']}", "message": rows["message_id"],
                    "error": f"Run error {rows['tag']}",
                },
            )
            rows["notification_id"] = str(await conn.scalar(
                text(
                    "INSERT INTO sync_notifications (account_id, action, error, detail) "
                    "VALUES (:account, 'flag_add', :error, '{}'::jsonb) RETURNING id"
                ),
                {"account": rows["account_id"], "error": f"Notification {rows['tag']}"},
            ))
            await conn.execute(
                text(
                    "INSERT INTO image_exceptions (id, account_id, exception_type, value) "
                    "VALUES (:id, :account, 'sender', :value)"
                ),
                {
                    "id": rows["exception_id"], "account": rows["account_id"],
                    "value": f"images-{rows['tag']}@example.com",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO pending_sends (id, account_id, from_addr, to_addrs, subject, "
                    "body_text, send_after) VALUES (:id, :account, :email, '[\"x@example.com\"]', "
                    ":subject, 'staged', now() + interval '1 day')"
                ),
                {
                    "id": rows["pending_send_id"], "account": rows["account_id"],
                    "email": rows["email"], "subject": f"Staged {rows['tag']}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO pending_send_attachments (id, pending_send_id, filename, data) "
                    "VALUES (:id, :pending, :name, 'bytes')"
                ),
                {
                    "id": rows["pending_attachment_id"], "pending": rows["pending_send_id"],
                    "name": f"staged-{rows['tag']}.txt",
                },
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def world(
    app_client: TestClient,
    auth_issuer: TestIssuer,
    postgres_url: str,
    dovecot_endpoint: tuple[str, int, int],
    radicale_endpoint: tuple[str, int],
) -> World:
    """Users A and B with one mail and one DAV account each, and B's data."""
    tag = uuid.uuid4().hex[:8]
    owner_token = auth_issuer.mint(OWNER_SUBJECT)
    a_token = auth_issuer.mint(f"isolation-a-{tag}", email=f"a-{tag}@users.test", name="User A")
    b_token = auth_issuer.mint(f"isolation-b-{tag}", email=f"b-{tag}@users.test", name="User B")
    a_user_id = app_client.get("/api/me", headers=_as(a_token)).json()["user_id"]
    b_user_id = app_client.get("/api/me", headers=_as(b_token)).json()["user_id"]

    a_email, b_email = unique_email("iso-a"), unique_email("iso-b")
    b_subject = f"Isolation secret {tag}"
    b_sender = f"secret-sender-{tag}@example.com"
    host, _imap_port, lmtp_port = dovecot_endpoint
    deliver_message(
        build_eml(sender=b_sender, recipient=b_email, subject=b_subject,
                  message_id=f"<isolation-{tag}@example.com>"),
        host, lmtp_port, sender=b_sender, recipient=b_email,
    )
    a_account_id = _add_mail_account(app_client, a_token, a_email)
    b_account_id = _add_mail_account(app_client, b_token, b_email)

    def _b_inbox() -> str | None:
        folders = app_client.get(f"/api/accounts/{b_account_id}/folders", headers=_as(b_token))
        return next((f["id"] for f in folders.json() if f["imap_name"] == "INBOX"), None)

    folder_id = _poll(_b_inbox, "B's INBOX being discovered")

    def _b_message() -> str | None:
        listing = app_client.get(
            f"/api/accounts/{b_account_id}/messages", headers=_as(b_token),
            params={"folder_id": folder_id},
        )
        return next(
            (m["id"] for m in listing.json()["messages"] if m["subject"] == b_subject), None,
        )

    message_id = _poll(_b_message, "B's message being synced")

    radicale_host, radicale_port = radicale_endpoint
    b_dav_user = f"iso-b-{tag}"
    b_contact_name, b_contact_email = f"B contact {tag}", f"b-contact-{tag}@example.com"
    with httpx.Client(auth=(b_dav_user, "unused"), timeout=10.0) as dav:
        principal = discover(dav, f"http://{radicale_host}:{radicale_port}/")
        calendar_url = create_calendar(dav, principal, "b", f"B cal {tag}")
        put_object(dav, f"{calendar_url}isolation-{tag}.ics",
                   sample_event(f"isolation-{tag}", b_subject), "text/calendar; charset=utf-8")
        book_url = create_addressbook(dav, principal, "b-book", f"B book {tag}")
        # A category and an embedded photo, so the groups index, the photo
        # index and the photo route each have something of B's to leak.
        card = sample_contact(f"isolation-{tag}", b_contact_name, b_contact_email).replace(
            "END:VCARD",
            f"CATEGORIES:B group {tag}\r\nPHOTO;ENCODING=b;TYPE=PNG:{_PIXEL}\r\nEND:VCARD",
        )
        put_object(dav, f"{book_url}isolation-{tag}.vcf", card, "text/vcard; charset=utf-8")
    a_dav_account_id = _add_dav_account(app_client, a_token, f"iso-a-{tag}")
    b_dav_account_id = _add_dav_account(app_client, b_token, b_dav_user)

    def _b_calendar() -> str | None:
        account = app_client.get(f"/api/dav-accounts/{b_dav_account_id}", headers=_as(b_token))
        return next(
            (c["id"] for c in account.json()["collections"] if c["kind"] == "calendar"), None,
        )

    calendar_id = _poll(_b_calendar, "B's calendar being discovered")

    def _b_addressbook() -> str | None:
        account = app_client.get(f"/api/dav-accounts/{b_dav_account_id}", headers=_as(b_token))
        return next(
            (c["id"] for c in account.json()["collections"] if c["kind"] == "addressbook"), None,
        )

    addressbook_id = _poll(_b_addressbook, "B's address book being discovered")

    def _b_event() -> str | None:
        month = app_client.get(
            "/api/calendar/events", headers=_as(b_token),
            params={"month": SAMPLE_EVENT_MONTH, "calendars": calendar_id},
        )
        return next((e["object_id"] for e in month.json()["events"]), None)

    event_id = _poll(_b_event, "B's event being synced")

    def _b_contact() -> str | None:
        listing = app_client.get(
            "/api/contacts", headers=_as(b_token), params={"addressbook_id": addressbook_id},
        )
        return next((c["id"] for c in listing.json()["contacts"]), None)

    contact_id = _poll(_b_contact, "B's contact being synced")

    rows: dict[str, Any] = {
        "tag": tag, "b_user_id": b_user_id, "account_id": b_account_id,
        "folder_id": folder_id, "message_id": message_id, "subject": b_subject,
        "email": b_email, "sender": b_sender, "view_id": str(uuid.uuid4()),
        "alert_id": str(uuid.uuid4()), "order_id": str(uuid.uuid4()),
        "subscription_id": str(uuid.uuid4()), "identity_id": str(uuid.uuid4()),
        "verdict_id": str(uuid.uuid4()), "run_id": str(uuid.uuid4()),
        "exception_id": str(uuid.uuid4()), "pending_send_id": str(uuid.uuid4()),
        "pending_attachment_id": str(uuid.uuid4()),
    }
    asyncio.run(_seed_owned_rows(postgres_url, rows))

    b = {
        "account_id": b_account_id, "dav_account_id": b_dav_account_id,
        "folder_id": folder_id, "message_id": message_id, "calendar_id": calendar_id,
        "addressbook_id": addressbook_id, "event_id": event_id, "contact_id": contact_id,
        "contact_email": b_contact_email,
        "sign_in_id": app_client.get("/api/me/sign-ins", headers=_as(b_token)).json()[0]["id"],
        **{key: rows[key] for key in (
            "view_id", "alert_id", "order_id", "subscription_id", "identity_id",
            "pipeline_revision", "verdict_id", "run_id", "exception_id", "pending_send_id",
            "pending_attachment_id", "notification_id",
        )},
    }
    return World(
        owner_token=owner_token, a_token=a_token, b_token=b_token,
        a_user_id=a_user_id, b_user_id=b_user_id,
        a_account_id=a_account_id, b_account_id=b_account_id,
        a_dav_account_id=a_dav_account_id, b_dav_account_id=b_dav_account_id,
        b=b,
        b_markers=frozenset({
            b_user_id, b_email, b_subject, b_sender, f"B cal {tag}", f"B view {tag}",
            f"Merchant {tag}", f"B book {tag}", f"B group {tag}", b_contact_name,
            f"Reasoning {tag}", f"Run error {tag}", f"Notification {tag}", f"Staged {tag}",
            f"images-{tag}@example.com", f"staged-{tag}.txt",
            # A bare counter and a revision number would match anything.
            *(
                value for key, value in b.items()
                if key not in ("pipeline_revision", "notification_id")
            ),
        }),
        b_content=frozenset({b_subject, b_sender}),
    )


def _leaked(body: str, markers: frozenset[str]) -> list[str]:
    return sorted(marker for marker in markers if marker in body)


@pytest.mark.parametrize("case", route_cases(), ids=lambda case: case.route)
def test_route(app_client: TestClient, world: World, case: RouteCase) -> None:
    method, path = fill_path(case.route, case.params(world) if case.params else {})
    kwargs: dict[str, Any] = {"follow_redirects": False}
    if case.query:
        kwargs["params"] = case.query(world)
    if case.body:
        kwargs["json"] = case.body(world)
    if case.files:
        kwargs["files"] = case.files(world)

    def call(headers: dict[str, str]) -> Any:
        return app_client.request(method, path, headers=headers, **kwargs)

    if case.kind == "public":
        response = call(_NO_TOKEN)
        assert response.status_code != 401, response.text
    elif case.kind == "own":
        response = call(_as(world.a_token))
        assert 200 <= response.status_code < 300 or response.status_code in case.also, (
            response.text
        )
        assert not _leaked(response.text, world.b_markers)
    elif case.kind == "stream":
        assert call(_NO_TOKEN).status_code == 401
    elif case.kind in ("account_path", "resource_id"):
        response = call(_as(world.a_token))
        assert response.status_code in (403, 404), response.text
    else:
        refused = call(_as(world.a_token))
        assert refused.status_code == 403, refused.text
        allowed = call(_as(world.owner_token))
        assert (
            200 <= allowed.status_code < 300 or allowed.status_code in case.owner_also
        ), allowed.text
        assert not _leaked(allowed.text, world.b_content)


def test_tools(app_client: TestClient, world: World) -> None:
    """Every classified MCP tool, called as A."""
    session = McpHttpSession(app_client, "/mcp/", _as(world.a_token))
    for case in tool_cases():
        message = session.call_tool(case.tool, case.arguments(world), _as(world.a_token))
        if case.kind == "own":
            assert "error" not in message and not message["result"].get("isError"), message
        leaked = _leaked(json.dumps(message), world.b_markers)
        assert not leaked, f"{case.tool} returned B's data to A: {leaked}"


def test_checks(app_client: TestClient, world: World) -> None:
    """Every free-form check an area module adds."""
    for check in checks():
        check(app_client, world)


def test_the_world_holds_what_it_claims(app_client: TestClient, world: World) -> None:
    """B's data really carries B's markers -- otherwise every "nothing of
    B's appears" assertion above would pass on an empty world."""
    listing = app_client.get(
        f"/api/accounts/{world.b_account_id}/messages", headers=_as(world.b_token),
        params={"folder_id": world.b["folder_id"]},
    )

    assert listing.status_code == 200
    assert world.b_content <= set(_leaked(listing.text, world.b_markers))
