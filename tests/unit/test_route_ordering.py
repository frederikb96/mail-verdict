"""Route registration order in the composed application.

Starlette matches routes in registration order and a Mount claims every path
beneath it, so a route registered after `app.mount("/api", ...)` but sharing its
prefix is unreachable. The endpoint exists, imports fine, and answers 404 --
which looks like a missing route rather than a shadowed one.

The SSE endpoint is the case that matters: it lives on the root app rather than
inside the mounted API app, because it is a plain Starlette route rather than a
FastAPI one.
"""

from __future__ import annotations

import os

import pytest
from starlette.routing import Mount, Route

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


@pytest.fixture(scope="module")
def routes() -> list[object]:
    """The composed root application's route table."""
    from mail_verdict.server import create_app

    return list(create_app().routes)


def _index_of_sse(routes: list[object]) -> int:
    """Return the position of the SSE route."""
    for index, route in enumerate(routes):
        if isinstance(route, Route) and route.path == "/api/events":
            return index
    raise AssertionError("no /api/events route is registered on the root application")


def _index_of_api_mount(routes: list[object]) -> int:
    """Return the position of the /api mount."""
    for index, route in enumerate(routes):
        if isinstance(route, Mount) and route.path == "/api":
            return index
    raise AssertionError("no /api mount is registered on the root application")


def test_sse_route_is_registered_before_the_api_mount(routes: list[object]) -> None:
    """The SSE route must win against the mount that shares its prefix."""
    sse = _index_of_sse(routes)
    mount = _index_of_api_mount(routes)

    assert sse < mount, (
        "/api/events is registered after the /api mount, so the mount matches "
        "first and the endpoint answers 404"
    )


def test_the_api_mount_does_not_itself_serve_events(routes: list[object]) -> None:
    """The SSE endpoint belongs to the root app, not the mounted API app.

    If it ever moves inside the mount, the ordering test above stops meaning
    anything -- so assert the arrangement it is protecting.
    """
    mount = routes[_index_of_api_mount(routes)]
    assert isinstance(mount, Mount)

    mounted_paths = {getattr(route, "path", None) for route in mount.routes}
    assert "/events" not in mounted_paths
