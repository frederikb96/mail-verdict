"""Orders and tickets: every order, file and sweep belongs to the user whose
mail or upload made it, and no one else's calls reach it."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from starlette.testclient import TestClient

from tests.e2e.isolation import RouteCase, ToolCase, World
from tests.setup.auth import bearer

_PDF = b"%PDF-1.4\nisolation file\n"


def _b_order(world: World) -> dict[str, str]:
    return {"order_id": world.b["order_id"]}


def _b_order_and(*names: str) -> Callable[[World], dict[str, str]]:
    """B's order id with fresh ids for the rows beneath it (none exist, so
    the answer can only be "not found")."""

    def build(world: World) -> dict[str, str]:
        return {**_b_order(world), **{name: str(uuid.uuid4()) for name in names}}

    return build


def _upload(world: World) -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("isolation.pdf", _PDF, "application/pdf")}


ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/orders", "own"),
    RouteCase("POST /api/orders/files", "own", files=_upload),
    RouteCase(
        "GET /api/orders/files/{file_id}", "resource_id",
        params=lambda world: {"file_id": str(uuid.uuid4())},
    ),
    RouteCase(
        "POST /api/orders/catch-up", "account_path",
        body=lambda world: {"account_id": world.b_account_id, "days": 7, "dry_run": True},
    ),
    RouteCase("GET /api/orders/{order_id}", "resource_id", params=_b_order),
    RouteCase(
        "PATCH /api/orders/{order_id}", "resource_id", params=_b_order,
        body=lambda world: {"is_favorite": True},
    ),
    RouteCase("DELETE /api/orders/{order_id}", "resource_id", params=_b_order),
    RouteCase("POST /api/orders/{order_id}/rewrite", "resource_id", params=_b_order),
    RouteCase(
        "POST /api/orders/{order_id}/merge", "resource_id", params=_b_order,
        body=lambda world: {"into": str(uuid.uuid4())},
    ),
    RouteCase(
        "POST /api/orders/{order_id}/mails/{mail_key}/detach", "resource_id",
        params=_b_order_and("mail_key"), body=lambda world: {},
    ),
    RouteCase(
        "GET /api/orders/{order_id}/files/{file_id}", "resource_id",
        params=_b_order_and("file_id"),
    ),
    RouteCase(
        "DELETE /api/orders/{order_id}/files/{file_id}", "resource_id",
        params=_b_order_and("file_id"),
    ),
    RouteCase(
        "GET /api/orders/{order_id}/codes/{fact_id}.png", "resource_id",
        params=_b_order_and("fact_id"),
    ),
]

TOOL_CASES: list[ToolCase] = [
    ToolCase("list_orders", "own"),
    ToolCase("get_order", "resource_id", arguments=lambda world: {"order_id": world.b["order_id"]}),
]


def an_upload_of_b_stays_b(client: TestClient, world: World) -> None:
    """A file B added is not visible to A, A uploading the same bytes gets a
    file of their own, and A cannot add a file to B's order."""
    mine, theirs = bearer(world.a_token), bearer(world.b_token)
    files = {"file": ("b-private.pdf", _PDF + b"b-only", "application/pdf")}

    added = client.post("/api/orders/files", headers=theirs, files=files)
    assert added.status_code == 202, added.text
    file_id = added.json()["file_id"]

    assert client.get(f"/api/orders/files/{file_id}", headers=theirs).status_code == 200
    assert client.get(f"/api/orders/files/{file_id}", headers=mine).status_code == 404

    same_bytes = client.post("/api/orders/files", headers=mine, files=files)
    assert same_bytes.status_code == 202, same_bytes.text
    assert same_bytes.json()["file_id"] != file_id

    into_theirs = client.post(
        "/api/orders/files", headers=mine, files=files, data={"order_id": world.b["order_id"]},
    )
    assert into_theirs.status_code == 404, into_theirs.text


CHECKS = [an_upload_of_b_stays_b]
