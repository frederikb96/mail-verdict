"""
The authentication gate, users and account ownership, on the running app.

Every route under /api and /mcp refuses a request without a valid token,
derived from the live route list so a new route is covered the day it
lands. Behind the gate: who /api/me says the caller is, how a new subject
becomes a user, how an admin disables one or removes one of the owner's
subjects, how a phone, a browser or a machine client is listed and revoked
on its own, and that creating an account makes the caller its owner.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from mail_verdict.auth.bootstrap import ensure_owner
from mail_verdict.auth.middleware import PUBLIC_PATHS
from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import get_db_connection
from tests.e2e.isolation import UI_ROUTES, enumerate_routes
from tests.setup.auth import NATIVE_CLIENT_ID, OWNER_SUBJECT, TestIssuer, bearer
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


def _hold_stream(app_client: TestClient, token: str, opened: threading.Event) -> Any:
    """Start GET /api/events as `token`'s caller, through the whole app,
    until the app itself ends the response (or 20 s pass). Returns the
    future of everything the stream carried; `opened` is set once it has
    sent `connected`."""

    async def hold() -> str:
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

    return app_client.portal.start_task_soon(hold)


def _phone_token(
    auth_issuer: TestIssuer, subject: str, session: str, *, token_id: str = "at_1",
) -> str:
    """An access token of the native app (auth.native_client_id is
    NATIVE_CLIENT_ID in every test session) for one sign-in."""
    return auth_issuer.mint(
        subject, extra={"azp": NATIVE_CLIENT_ID, "jti": f"{session}-{token_id}"},
    )


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
        assert config.json()["client_id"] == NATIVE_CLIENT_ID


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

        stream = _hold_stream(app_client, token, opened)
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

    def test_an_admin_removes_one_of_the_owners_subjects_for_good(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        """An owner subject is an ordinary row: removed, its token is
        refused, and a restart does not link it back from configuration --
        the owner still has another subject, so configuration is not read."""
        agent = f"owner-agent-{uuid.uuid4()}"
        token = auth_issuer.mint(agent)
        added = app_client.post(
            f"/api/admin/users/{OWNER_USER_ID}/subjects", json={"subject": agent},
        )
        as_owner = app_client.get("/api/me", headers=bearer(token))

        removed = app_client.delete(f"/api/admin/users/{OWNER_USER_ID}/subjects/{agent}")
        app_client.portal.call(ensure_owner, get_db_connection(), [OWNER_SUBJECT, agent])
        owner = next(
            user for user in app_client.get("/api/admin/users").json() if user["is_owner"]
        )
        refused = app_client.get("/api/me", headers=bearer(token))

        assert added.status_code == 201, added.text
        assert as_owner.json()["user_id"] == str(OWNER_USER_ID)
        assert removed.status_code == 200
        assert agent not in removed.json()["subjects"]
        assert refused.status_code == 401
        assert refused.json() == {"detail": "device_revoked"}
        assert agent not in owner["subjects"]

    def test_the_subject_a_request_signed_in_with_cannot_be_removed_by_it(
        self, app_client: TestClient,
    ) -> None:
        response = app_client.delete(
            f"/api/admin/users/{OWNER_USER_ID}/subjects/{OWNER_SUBJECT}",
        )

        assert response.status_code == 400


class TestSignIns:
    def test_a_token_living_longer_than_the_ceiling_is_refused(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        century = auth_issuer.mint(OWNER_SUBJECT, expires_in=100 * 365 * 86400)

        assert app_client.get("/api/me", headers=bearer(century)).status_code == 401

    def test_a_phone_is_listed_under_its_name_and_revoked_alone(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        subject = f"phone-owner-{uuid.uuid4()}"
        pixel = _phone_token(auth_issuer, subject, "V2_101")
        iphone = _phone_token(auth_issuer, subject, "V2_102")
        app_client.get("/api/me", headers={**bearer(pixel), "X-Device-Name": "Pixel 8"})
        app_client.get("/api/me", headers={**bearer(iphone), "X-Device-Name": "iPhone"})

        listed = app_client.get("/api/me/sign-ins", headers=bearer(pixel)).json()
        by_name = {row["name"]: row for row in listed}
        revoked = app_client.post(
            f"/api/me/sign-ins/{by_name['iPhone']['id']}/revoke", headers=bearer(pixel),
        )
        refreshed = _phone_token(auth_issuer, subject, "V2_102", token_id="at_2")
        refused = app_client.get("/api/me", headers=bearer(refreshed))
        still = app_client.get("/api/me", headers=bearer(pixel))
        signed_in_again = app_client.get(
            "/api/me", headers=bearer(_phone_token(auth_issuer, subject, "V2_103")),
        )

        assert set(by_name) == {"Pixel 8", "iPhone"}
        assert {row["kind"] for row in listed} == {"phone"}
        assert by_name["Pixel 8"]["current"] and not by_name["iPhone"]["current"]
        assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
        assert refused.status_code == 401
        assert refused.json() == {"detail": "device_revoked"}
        assert still.status_code == 200
        assert signed_in_again.status_code == 200

    def test_a_native_token_without_a_session_handle_is_refused(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        token = auth_issuer.mint(
            f"odd-phone-{uuid.uuid4()}", extra={"azp": NATIVE_CLIENT_ID, "jti": "opaque"},
        )

        response = app_client.get("/api/me", headers=bearer(token))

        assert response.status_code == 401
        assert response.json() == {"detail": "device_unrecognised"}

    def test_nobody_revokes_someone_elses_sign_in_but_an_admin(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        victim = _phone_token(auth_issuer, f"victim-{uuid.uuid4()}", "V2_201")
        other = auth_issuer.mint(f"other-{uuid.uuid4()}")
        app_client.get("/api/me", headers=bearer(victim))
        sign_in = app_client.get("/api/me/sign-ins", headers=bearer(victim)).json()[0]

        foreign = app_client.post(
            f"/api/me/sign-ins/{sign_in['id']}/revoke", headers=bearer(other),
        )
        foreign_admin = app_client.post(
            f"/api/admin/sign-ins/{sign_in['id']}/revoke", headers=bearer(other),
        )
        alive = app_client.get("/api/me", headers=bearer(victim))
        by_admin = app_client.post(f"/api/admin/sign-ins/{sign_in['id']}/revoke")
        gone = app_client.get("/api/me", headers=bearer(victim))

        assert foreign.status_code == 403
        assert foreign_admin.status_code == 403
        assert alive.status_code == 200
        assert by_admin.status_code == 200
        assert gone.status_code == 401

    def test_signing_a_person_out_everywhere_revokes_phones_browsers_and_machines(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        subject = f"everywhere-{uuid.uuid4()}"
        phone = _phone_token(auth_issuer, subject, "V2_301")
        signed_in = int(time.time()) - 60
        browser = auth_issuer.mint(subject, extra={"auth_time": signed_in, "sid": "sso"})
        user_id = app_client.get("/api/me", headers=bearer(phone)).json()["user_id"]
        machine_subject = f"everywhere-key-{uuid.uuid4()}"
        app_client.post(
            f"/api/admin/users/{user_id}/subjects", json={"subject": machine_subject},
        )
        machine = auth_issuer.mint(machine_subject)
        for token in (browser, machine):
            app_client.get("/api/me", headers=bearer(token))
        kinds = {
            row["kind"]
            for row in app_client.get("/api/admin/sign-ins", params={"user_id": user_id}).json()
        }

        signed_out = app_client.post(f"/api/admin/users/{user_id}/sign-out")
        refused = {
            name: app_client.get("/api/me", headers=bearer(token)).status_code
            for name, token in (("phone", phone), ("browser", browser), ("machine", machine))
        }
        fresh_browser = auth_issuer.mint(
            subject, extra={"auth_time": int(time.time()) + 5, "sid": "sso"},
        )
        back = app_client.get("/api/me", headers=bearer(fresh_browser))

        assert kinds == {"phone", "browser", "machine"}
        assert signed_out.json() == {"revoked": 3}
        assert refused == {"phone": 401, "browser": 401, "machine": 401}
        assert back.status_code == 200

    def test_revoking_a_sign_in_ends_its_open_event_stream(
        self, app_client: TestClient, auth_issuer: TestIssuer,
    ) -> None:
        token = _phone_token(auth_issuer, f"streaming-phone-{uuid.uuid4()}", "V2_401")
        sign_in_id = (
            app_client.get("/api/me/sign-ins", headers=bearer(token)).json()[0]["id"]
        )
        opened = threading.Event()

        stream = _hold_stream(app_client, token, opened)
        assert opened.wait(15), "the stream never opened"
        revoked = app_client.post(f"/api/admin/sign-ins/{sign_in_id}/revoke")
        carried = stream.result(timeout=30)

        assert revoked.status_code == 200
        assert "event: connected" in carried


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
