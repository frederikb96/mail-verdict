"""Identity, sign-in discovery, readiness, the API documents and the admin routes."""

from __future__ import annotations

import uuid

from tests.e2e.isolation import RouteCase, World


def _b_user(world: World) -> dict[str, str]:
    return {"user_id": world.b_user_id}


ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/health", "public"),
    RouteCase("GET /api/auth/config", "public"),
    *(RouteCase(f"{method} /mcp", "public") for method in ("GET", "POST", "DELETE")),
    RouteCase("GET /api/me", "own"),
    RouteCase("GET /api/openapi.json", "own"),
    RouteCase("GET /api/docs", "own"),
    RouteCase("GET /api/docs/oauth2-redirect", "own"),
    RouteCase("GET /api/redoc", "own"),
    RouteCase("GET /api/admin/users", "admin"),
    RouteCase(
        "POST /api/admin/users", "admin",
        body=lambda world: {"subject": f"pre-created-{uuid.uuid4()}", "display_name": "Guest"},
    ),
    RouteCase(
        "PATCH /api/admin/users/{user_id}", "admin",
        params=_b_user, body=lambda world: {"display_name": "B"},
    ),
    RouteCase("GET /api/admin/accounts", "admin"),
    RouteCase(
        "PUT /api/admin/accounts/{account_id}/owner", "admin",
        params=lambda world: {"account_id": world.b_account_id}, body=_b_user,
    ),
    RouteCase("GET /api/admin/dav-accounts", "admin"),
    RouteCase(
        "PUT /api/admin/dav-accounts/{dav_account_id}/owner", "admin",
        params=lambda world: {"dav_account_id": world.b_dav_account_id}, body=_b_user,
    ),
]
