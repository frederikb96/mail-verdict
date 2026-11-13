"""Settings, account order, and the admin-only machinery: secrets, webhooks,
queues, provider status, embedding coverage and backfill."""

from __future__ import annotations

import uuid
from typing import Any

from starlette.testclient import TestClient

from mail_verdict.settings.defaults import SettingCategory, user_keys
from tests.e2e.isolation import RouteCase, World
from tests.setup.auth import bearer

_SECRET = "isolation-secret"


def _order(world: World) -> dict[str, Any]:
    return {"order": [world.b_account_id, world.a_account_id]}


def _mail_setting(world: World) -> dict[str, Any]:
    return {"data": {"bell_badge_counts_new_mail": True}}


def _category_mail(world: World) -> dict[str, str]:
    return {"category": "mail"}


def _queue(world: World) -> dict[str, str]:
    return {"name": "embeddings"}


def _secret(world: World) -> dict[str, str]:
    return {"name": _SECRET}


def _webhook(world: World) -> dict[str, str]:
    return {"name": "isolation-hook"}


ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/account-order", "own"),
    RouteCase("PUT /api/account-order", "own", body=_order),
    RouteCase("GET /api/settings", "own"),
    RouteCase("GET /api/settings/schema", "own"),
    RouteCase("GET /api/settings/{category}", "own", params=_category_mail),
    RouteCase("PUT /api/settings/{category}", "own", params=_category_mail, body=_mail_setting),
    RouteCase("POST /api/settings/import", "admin", body=lambda world: {"data": {}}),
    RouteCase("GET /api/settings/providers", "admin"),
    RouteCase("GET /api/secrets", "admin"),
    RouteCase(
        "PUT /api/secrets/{name}", "admin", params=_secret,
        body=lambda world: {"value": "unused"}, owner_also=(400,),
    ),
    RouteCase("DELETE /api/secrets/{name}", "admin", params=_secret, owner_also=(404,)),
    RouteCase("GET /api/queues", "admin"),
    RouteCase("GET /api/queues/{name}", "admin", params=_queue, owner_also=(404,)),
    RouteCase(
        "PATCH /api/queues/{name}", "admin", params=_queue, body=lambda world: {},
        owner_also=(404,),
    ),
    RouteCase("GET /api/webhooks/deliveries", "admin"),
    RouteCase(
        "POST /api/webhooks/deliveries/{delivery_id}/retry", "admin",
        params=lambda world: {"delivery_id": str(uuid.uuid4())}, owner_also=(409,),
    ),
    RouteCase("POST /api/webhooks/{name}/retry-failed", "admin", params=_webhook),
    RouteCase(
        "POST /api/webhooks/{name}/backfill", "admin", params=_webhook,
        body=lambda world: {"since": "2020-01-01T00:00:00Z", "dry_run": True},
        owner_also=(404, 409),
    ),
    RouteCase("GET /api/embeddings/status", "admin"),
    RouteCase(
        "POST /api/embeddings/backfill", "admin",
        query=lambda world: {"account_id": world.a_account_id},
    ),
]


def check_a_user_sees_and_sets_only_their_own_keys(client: TestClient, world: World) -> None:
    """An ordinary user's view holds user keys alone, an admin category is
    refused, a write of an instance key changes nothing, and a user's own
    value is not B's."""
    a, b = bearer(world.a_token), bearer(world.b_token)

    overview = client.get("/api/settings", headers=a).json()
    assert {c: set(keys) for c, keys in overview.items()} == {
        c.value: set(user_keys(c.value)) for c in SettingCategory if user_keys(c.value)
    }
    for category in ("ai", "assistant", "retry", "pipeline", "semantic"):
        assert client.get(f"/api/settings/{category}", headers=a).status_code == 403
    assert set(client.get("/api/settings/orders", headers=a).json()) == set(user_keys("orders"))

    refused = client.put(
        "/api/settings/orders", headers=a,
        json={"data": {"language": "Dutch", "decide_model": "x"}},
    )
    assert refused.status_code == 403
    assert client.get("/api/settings/orders", headers=a).json()["language"] == "English"

    assert client.put(
        "/api/settings/orders", headers=a, json={"data": {"language": "Dutch"}},
    ).status_code == 200
    assert client.get("/api/settings/orders", headers=a).json()["language"] == "Dutch"
    assert client.get("/api/settings/orders", headers=b).json()["language"] == "English"
    client.put("/api/settings/orders", headers=a, json={"data": {"language": "English"}})


def check_a_provider_key_from_a_user_is_refused(client: TestClient, world: World) -> None:
    """A provider key is an instance key: a user's attempt is a 403 whichever
    category carries it."""
    a = bearer(world.a_token)

    for category in ("ai", "mail"):
        response = client.put(
            f"/api/settings/{category}", headers=a,
            json={"data": {"openai_api_key": "sk-isolation-never-stored"}},
        )
        assert response.status_code == 403, response.text


def check_the_account_order_of_a_user_names_only_their_accounts(
    client: TestClient, world: World,
) -> None:
    """B's account id in A's order is dropped when saved and never read back;
    B's own order stays B's."""
    a, b = bearer(world.a_token), bearer(world.b_token)

    saved = client.put("/api/account-order", headers=a, json=_order(world)).json()
    assert saved == {"order": [world.a_account_id]}
    assert client.get("/api/account-order", headers=b).json() == {"order": []}
    mixed = client.put(
        "/api/settings/mail", headers=a, json={"data": {"account_order": [world.b_account_id]}},
    ).json()
    assert mixed["account_order"] == []


CHECKS = [
    check_a_user_sees_and_sets_only_their_own_keys,
    check_a_provider_key_from_a_user_is_refused,
    check_the_account_order_of_a_user_names_only_their_accounts,
]
