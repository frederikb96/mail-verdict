"""
The authentication gate, users and account ownership, on the running app.

Every route under /api and /mcp refuses a request without a valid token,
derived from the live route list so a new route is covered the day it
lands. Behind the gate: who /api/me says the caller is, how a new subject
becomes a user, how an admin disables one, that the owner still signs in
with no identity rows at all, and that creating an account makes the caller
its owner.
"""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from mail_verdict.auth.middleware import PUBLIC_PATHS
from mail_verdict.auth.principal import OWNER_USER_ID, get_principal_resolver
from tests.e2e.isolation import UI_ROUTES, enumerate_routes
from tests.setup.auth import OWNER_SUBJECT, TestIssuer, bearer
from tests.setup.containers import RADICALE_ALIAS, RADICALE_PORT

_NO_TOKEN = {"Authorization": ""}
_ANY_ID = "00000000-0000-0000-0000-00000000abcd"


async def _rows(postgres_url: str, query: str, **params: Any) -> list[Any]:
    engine = create_async_engine(postgres_url)
    try:
        async with engine.begin() as conn:
            return list((await conn.execute(text(query), params)).all())
    finally:
        await engine.dispose()


def _gated_requests(app: Any) -> list[tuple[str, str]]:
    """(method, concrete path) for every route but the public ones, any id
    filled in -- the gate answers before a path parameter is ever parsed."""
    requests = []
    for entry in sorted(enumerate_routes(app) - UI_ROUTES):
        method, path = entry.split(" ", 1)
        if path not in PUBLIC_PATHS:
            requests.append((method, re.sub(r"\{[^}]+\}", _ANY_ID, path)))
    return requests


class TestTheGate:
    def test_every_gated_route_refuses_a_request_without_a_token(
        self, app_client: TestClient,
    ) -> None:
        requests = _gated_requests(app_client.app)
        assert len(requests) > 100
        let_through = [
            f"{method} {path}: {response.status_code}"
            for method, path in requests
            if (response := app_client.request(
                method, path, headers=_NO_TOKEN, follow_redirects=False,
            )).status_code != 401
        ]

        assert not let_through

    def test_the_mcp_endpoint_refuses_a_request_without_a_token(
        self, app_client: TestClient,
    ) -> None:
        response = app_client.post(
            "/mcp/", headers=_NO_TOKEN,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("variant", ["expired", "other_audience", "other_issuer"])
    def test_a_token_that_is_not_valid_here_is_refused(
        self, app_client: TestClient, auth_issuer: TestIssuer, variant: str,
    ) -> None:
        token = {
            "expired": lambda: auth_issuer.mint(OWNER_SUBJECT, expires_in=-300),
            "other_audience": lambda: auth_issuer.mint(OWNER_SUBJECT, audience="elsewhere"),
            "other_issuer": lambda: auth_issuer.mint(OWNER_SUBJECT, issuer="https://x.test"),
        }[variant]()

        assert app_client.get("/api/me", headers=bearer(token)).status_code == 401

    def test_readiness_and_sign_in_discovery_answer_without_a_token(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        health = app_client.get("/api/health", headers=_NO_TOKEN)
        config = app_client.get("/api/auth/config", headers=_NO_TOKEN)

        assert health.status_code == 200
        assert config.status_code == 200
        assert config.json()["issuer"] == auth_issuer.issuer
        assert config.json()["client_id"] is None


class TestUsers:
    def test_the_owner_subject_is_the_owner_and_admin(self, app_client: TestClient) -> None:
        me = app_client.get("/api/me").json()

        assert me["user_id"] == str(OWNER_USER_ID)
        assert me["role"] == "admin"

    def test_a_new_subject_becomes_one_ordinary_user(
        self, app_client: TestClient, auth_issuer: TestIssuer, postgres_url: str,
    ) -> None:
        subject = f"newcomer-{uuid.uuid4()}"
        token = auth_issuer.mint(subject, email="newcomer@users.test", name="Newcomer")

        first = app_client.get("/api/me", headers=bearer(token)).json()
        second = app_client.get("/api/me", headers=bearer(token)).json()

        assert first == second
        assert first["role"] == "user"
        assert (first["email"], first["display_name"]) == ("newcomer@users.test", "Newcomer")
        identities = asyncio.run(_rows(
            postgres_url, "SELECT user_id FROM user_identities WHERE subject = :s", s=subject,
        ))
        assert [str(row[0]) for row in identities] == [first["user_id"]]

    def test_admin_routes_refuse_an_ordinary_user(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        token = auth_issuer.mint(f"ordinary-{uuid.uuid4()}")

        refused = app_client.get("/api/admin/users", headers=bearer(token))
        allowed = app_client.get("/api/admin/users")

        assert refused.status_code == 403
        assert allowed.status_code == 200
        assert any(user["is_owner"] for user in allowed.json())

    def test_a_disabled_user_is_refused_at_once_and_let_back_in_when_enabled(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        token = auth_issuer.mint(f"to-disable-{uuid.uuid4()}")
        user_id = app_client.get("/api/me", headers=bearer(token)).json()["user_id"]

        disabled = app_client.patch(f"/api/admin/users/{user_id}", json={"disabled": True})
        refused = app_client.get("/api/me", headers=bearer(token))
        app_client.patch(f"/api/admin/users/{user_id}", json={"disabled": False})
        back = app_client.get("/api/me", headers=bearer(token))

        assert disabled.status_code == 200
        assert refused.status_code == 403
        assert refused.json() == {"detail": "user_disabled"}
        assert back.status_code == 200

    def test_disabling_a_user_ends_their_open_event_stream(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        token = auth_issuer.mint(f"streaming-{uuid.uuid4()}")
        user_id = app_client.get("/api/me", headers=bearer(token)).json()["user_id"]
        opened = threading.Event()

        async def hold_stream() -> str:
            """GET /api/events as the user, through the whole app, until the
            app itself ends the response (or 20 s pass)."""
            received: list[str] = []
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": "/api/events",
                "raw_path": b"/api/events", "query_string": b"", "root_path": "",
                "headers": [
                    (b"host", b"testserver"), (b"authorization", f"Bearer {token}".encode()),
                ],
                "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            }
            never: asyncio.Event = asyncio.Event()

            async def receive() -> dict[str, Any]:
                await never.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.body":
                    received.append(message.get("body", b"").decode())
                    if "event: connected" in "".join(received):
                        opened.set()

            async with asyncio.timeout(20):
                await app_client.app(scope, receive, send)
            return "".join(received)

        stream = app_client.portal.start_task_soon(hold_stream)
        try:
            assert opened.wait(15), "the stream never opened"
            disabled = app_client.patch(f"/api/admin/users/{user_id}", json={"disabled": True})
            carried = stream.result(timeout=30)
        finally:
            app_client.patch(f"/api/admin/users/{user_id}", json={"disabled": False})

        assert disabled.status_code == 200
        assert "event: connected" in carried

    def test_the_owner_cannot_be_disabled_or_demoted(self, app_client: TestClient) -> None:
        for change in ({"disabled": True}, {"role": "user"}):
            response = app_client.patch(f"/api/admin/users/{OWNER_USER_ID}", json=change)
            assert response.status_code == 400

    def test_the_owner_signs_in_with_no_identity_rows_of_their_own(
        self, app_client: TestClient, postgres_url: str,
    ) -> None:
        asyncio.run(_rows(
            postgres_url, "DELETE FROM user_identities WHERE user_id = :o RETURNING subject",
            o=OWNER_USER_ID,
        ))
        resolver = get_principal_resolver()
        assert resolver is not None
        resolver.forget()

        me = app_client.get("/api/me")

        assert me.status_code == 200
        assert me.json()["role"] == "admin"


class TestOwnership:
    def test_a_new_mail_account_belongs_to_its_creator_under_the_name_they_chose(
        self, app_client: TestClient, auth_issuer: TestIssuer, postgres_url: str,
    ) -> None:
        token = auth_issuer.mint(f"creator-{uuid.uuid4()}")
        user_id = app_client.get("/api/me", headers=bearer(token)).json()["user_id"]

        created = app_client.post(
            "/api/accounts", headers=bearer(token),
            json={
                "name": "Work", "imap_host": "imap.invalid", "imap_port": 993,
                "imap_user": "u", "imap_password": "p", "is_active": False,
            },
        )
        assert created.status_code == 201, created.text
        account_id = created.json()["id"]
        stored = asyncio.run(_rows(
            postgres_url,
            "SELECT accounts.name, account_owners.user_id FROM accounts "
            "JOIN account_owners ON account_owners.account_id = accounts.id "
            "WHERE accounts.id = :id",
            id=account_id,
        ))
        deleted = app_client.delete(f"/api/accounts/{account_id}", headers=bearer(token))
        left = asyncio.run(_rows(
            postgres_url, "SELECT 1 FROM account_owners WHERE account_id = :id", id=account_id,
        ))

        assert created.json()["name"] == "Work"
        assert [(name, str(owner)) for name, owner in stored] == [(f"{user_id}/Work", user_id)]
        assert deleted.status_code == 204
        assert left == []

    def test_two_users_may_give_an_account_the_same_name(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        created = []
        for _ in range(2):
            token = auth_issuer.mint(f"namesake-{uuid.uuid4()}")
            response = app_client.post(
                "/api/accounts", headers=bearer(token),
                json={
                    "name": "Personal", "imap_host": "imap.invalid", "imap_port": 993,
                    "imap_user": "u", "imap_password": "p", "is_active": False,
                },
            )
            created.append((token, response))

        assert [response.status_code for _, response in created] == [201, 201]
        for token, response in created:
            app_client.delete(f"/api/accounts/{response.json()['id']}", headers=bearer(token))

    def test_a_new_dav_account_belongs_to_its_creator(
        self, app_client: TestClient, auth_issuer: TestIssuer, postgres_url: str,
    ) -> None:
        token = auth_issuer.mint(f"dav-creator-{uuid.uuid4()}")
        user_id = app_client.get("/api/me", headers=bearer(token)).json()["user_id"]

        created = app_client.post(
            "/api/dav-accounts", headers=bearer(token),
            json={
                "name": "Calendars", "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
                "username": f"gate-{uuid.uuid4().hex[:8]}", "password": "unused",
            },
        )
        assert created.status_code == 201, created.text
        dav_account_id = created.json()["id"]
        owners = asyncio.run(_rows(
            postgres_url,
            "SELECT user_id FROM dav_account_owners WHERE dav_account_id = :id",
            id=dav_account_id,
        ))
        deleted = app_client.delete(f"/api/dav-accounts/{dav_account_id}", headers=bearer(token))
        left = asyncio.run(_rows(
            postgres_url,
            "SELECT 1 FROM dav_account_owners WHERE dav_account_id = :id", id=dav_account_id,
        ))

        assert created.json()["name"] == "Calendars"
        assert [str(row[0]) for row in owners] == [user_id]
        assert deleted.status_code == 204
        assert left == []

    def test_an_admin_gives_an_account_to_another_user(
        self, app_client: TestClient, auth_issuer: TestIssuer, postgres_url: str,
    ) -> None:
        giver = auth_issuer.mint(f"giver-{uuid.uuid4()}")
        taker = auth_issuer.mint(f"taker-{uuid.uuid4()}")
        taker_id = app_client.get("/api/me", headers=bearer(taker)).json()["user_id"]
        account_id = app_client.post(
            "/api/accounts", headers=bearer(giver),
            json={
                "name": "Shared", "imap_host": "imap.invalid", "imap_port": 993,
                "imap_user": "u", "imap_password": "p", "is_active": False,
            },
        ).json()["id"]

        moved = app_client.put(
            f"/api/admin/accounts/{account_id}/owner", json={"user_id": taker_id},
        )
        stored = asyncio.run(_rows(
            postgres_url,
            "SELECT accounts.name, account_owners.user_id FROM accounts "
            "JOIN account_owners ON account_owners.account_id = accounts.id "
            "WHERE accounts.id = :id",
            id=account_id,
        ))
        app_client.delete(f"/api/accounts/{account_id}")

        assert moved.status_code == 200, moved.text
        assert moved.json()["name"] == "Shared"
        assert [(name, str(owner)) for name, owner in stored] == [(f"{taker_id}/Shared", taker_id)]
