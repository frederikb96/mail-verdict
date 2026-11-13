"""
The two-user isolation harness: what every route and MCP tool is, and how
tests/e2e/test_multiuser_isolation.py checks it.

Every route the application serves under /api and /mcp, and every MCP tool,
is classified exactly once -- by a case in one of the area modules beside
this file, or by an entry in pending.py. The harness enumerates the live app
and fails on anything unclassified, on a pending entry or case naming
something that no longer exists, and on anything classified twice. pending.py
only ever shrinks: an area that has been made per-user moves its routes out
of it into a case module of its own.

A case module (any module here but this one and pending.py) defines
`ROUTE_CASES: list[RouteCase]` and/or `TOOL_CASES: list[ToolCase]`. Each case
says what kind of thing a route is, and the harness checks it against a
World of two ordinary users, A and B, and the owner (admin): A is the
caller, B owns the data A must not reach.

Route kinds and what is checked:

- public: answers without a token (not 401).
- own: A calling it gets 2xx, and nothing of B's (World.b_markers) appears
  in the body.
- account_path: a path naming one of B's accounts, called by A: 403 or 404.
- resource_id: a path naming one of B's rows (a message, an order, ...),
  called by A: 403 or 404.
- admin: A (not admin) gets 403; the owner gets 2xx (or one of `owner_also`)
  and the body carries none of B's mail content (World.b_content).
- stream: a response that never ends (the live event stream), which a plain
  request cannot read to its end: only that it refuses a caller without a
  token (401) is checked here; what it carries for A is its module's CHECKS.

A case may need more than path parameters: `params` fills the path from the
World, `query`, `body` and `files` (a multipart upload) build the rest. A write
case must send a body that validates, or the request fails with 422 before the
access check ever runs.

Tool kinds: `own` (A's call succeeds and shows nothing of B's) and
`resource_id` (A's call naming B's data comes back an error, or without it).

What a case cannot express -- the live event stream, a replay by
Last-Event-ID, a body field naming someone else's folder -- goes in
`CHECKS: list[Check]`, plain functions given the app client and the World,
run by the harness like any case.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

# A free-form check: called with the app's TestClient and the World, it
# raises (asserts) on a leak.
Check = Callable[[Any, "World"], None]

RouteKind = Literal["public", "own", "account_path", "resource_id", "admin", "stream"]
ToolKind = Literal["own", "resource_id"]

# Routes outside /api and /mcp that the app may serve: the static web UI,
# present only when a UI build exists. Everything else must sit behind the
# gate, so a new route anywhere else fails the harness.
UI_ROUTES = frozenset({"GET /{path:path}", "MOUNT /_next"})


@dataclass(frozen=True)
class World:
    """Two ordinary users and what B owns.

    Attributes:
        owner_token / a_token / b_token: Bearer tokens.
        a_user_id / b_user_id: Their users rows.
        a_account_id / b_account_id: One mail account each.
        a_dav_account_id / b_dav_account_id: One DAV account each.
        b: Ids of B's data by name -- message_id, folder_id, calendar_id,
            addressbook_id, event_id, contact_id, order_id, alert_id,
            view_id, subscription_id, identity_id, pipeline_revision, ...
            -- for filling a path.
        b_markers: Strings that appear only in B's data (ids among them);
            none may show up in anything A receives.
        b_content: B's mail content (subject, sender), which no admin
            response may carry either.
    """

    owner_token: str
    a_token: str
    b_token: str
    a_user_id: str
    b_user_id: str
    a_account_id: str
    b_account_id: str
    a_dav_account_id: str
    b_dav_account_id: str
    b: dict[str, str]
    b_markers: frozenset[str]
    b_content: frozenset[str]


@dataclass(frozen=True)
class RouteCase:
    """How one route is checked. `route` is "METHOD /full/path/{template}"."""

    route: str
    kind: RouteKind
    params: Callable[[World], dict[str, str]] | None = None
    query: Callable[[World], dict[str, str]] | None = None
    body: Callable[[World], Any] | None = None
    # A multipart upload: field name -> (filename, bytes, content type).
    files: Callable[[World], dict[str, tuple[str, bytes, str]]] | None = None
    # Admin routes only: statuses the owner may get instead of 2xx, for a
    # route whose success needs state the World lacks (a delivery to retry,
    # an encryption key). Anything but a refusal proves the gate let the
    # owner through.
    owner_also: tuple[int, ...] = ()
    # Own routes only: the same, for A.
    also: tuple[int, ...] = ()


@dataclass(frozen=True)
class ToolCase:
    """How one MCP tool is checked."""

    tool: str
    kind: ToolKind
    arguments: Callable[[World], dict[str, Any]] = field(default=lambda world: {})


def _route_entries(route: Any, prefix: str) -> Iterable[str]:
    """The "METHOD /path" entries of one route, HEAD left out."""
    methods = getattr(route, "methods", None) or {"GET"}
    for method in sorted(methods - {"HEAD"}):
        yield f"{method} {prefix}{route.path}"


def _app_routes(routes: Iterable[Any], prefix: str) -> Iterable[str]:
    """The entries of a FastAPI app's routes. FastAPI may keep an included
    router as a wrapper around the original rather than copying its routes
    in; such a wrapper is followed, with the prefix it was included under."""
    for route in routes:
        if isinstance(route, APIRoute | Route):
            yield from _route_entries(route, prefix)
        elif hasattr(route, "original_router"):
            yield from _app_routes(
                route.original_router.routes, prefix + route.include_context.prefix,
            )


def enumerate_routes(app: Any) -> set[str]:
    """Every route the composed app serves, as "METHOD /path" -- the /api
    mount expanded into its own routes, the /mcp mount left to
    enumerate_tools(), any other mount as "MOUNT /path"."""
    entries: set[str] = set()
    for route in app.routes:
        if isinstance(route, Mount):
            if route.path == "/api":
                entries.update(_app_routes(route.app.routes, "/api"))  # type: ignore[union-attr]
            elif route.path != "/mcp":
                entries.add(f"MOUNT {route.path}")
        else:
            entries.update(_app_routes([route], ""))
    return entries


async def enumerate_tools() -> set[str]:
    """Every MCP tool the server offers."""
    from mail_verdict.api.mcp_tools import mcp

    return {tool.name for tool in await mcp.list_tools()}


def case_modules() -> list[Any]:
    """Every area module beside this file."""
    return [
        importlib.import_module(f"{__name__}.{info.name}")
        for info in pkgutil.iter_modules(__path__)
        if info.name != "pending"
    ]


def route_cases() -> list[RouteCase]:
    """Every route case of every area module."""
    return [case for module in case_modules() for case in getattr(module, "ROUTE_CASES", [])]


def tool_cases() -> list[ToolCase]:
    """Every tool case of every area module."""
    return [case for module in case_modules() for case in getattr(module, "TOOL_CASES", [])]


def checks() -> list[Check]:
    """Every free-form check of every area module."""
    return [check for module in case_modules() for check in getattr(module, "CHECKS", [])]


def fill_path(route: str, params: dict[str, str]) -> tuple[str, str]:
    """The method and concrete path of "METHOD /path/{name}" with `params`."""
    method, path = route.split(" ", 1)
    return method, path.format(**params)
