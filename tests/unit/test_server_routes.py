"""Routing guarantees of the composed ASGI app.

Both cases here are about an address a client is given by documentation
rather than one it discovers, so a wrong answer surfaces as a client that
cannot connect at all.
"""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x",
)


@pytest.fixture(scope="module")
def app_routes() -> TestClient:
    """The composed app, exercised for routing only.

    The lifespan is never entered, so nothing here touches a database.
    """
    from mail_verdict.server import create_app

    # create_app(), not the inner builder: the composed app ends with a
    # catch-all SPA route, and that catch-all is what swallows /mcp. Testing
    # the builder alone reproduces neither the bug nor the fix.
    return TestClient(create_app())


class TestMcpAddress:
    """The MCP server is documented as `/mcp`, and the mount answers `/mcp/`.

    Without an explicit route the bare path returns 405, which reads as a
    wrong method rather than a missing trailing slash -- so a client
    configured straight from the README fails with a misleading error.
    """

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_bare_mcp_path_redirects_to_the_mount(
        self, app_routes: TestClient, method: str,
    ) -> None:
        response = getattr(app_routes, method)("/mcp", follow_redirects=False)

        # 307 rather than 302: a POST carrying the JSON-RPC body must stay a
        # POST across the redirect, and 302 permits a client to downgrade it.
        assert response.status_code == 307
        # The test client absolutizes Location; only the path matters here.
        assert response.headers["location"].endswith("/mcp/")


class TestOpenApiVersion:
    """The served document must name the version of the package serving it."""

    def test_version_comes_from_the_package(self, app_routes: TestClient) -> None:
        from mail_verdict import __version__

        api_app = next(
            route.app  # type: ignore[attr-defined]
            for route in app_routes.app.routes  # type: ignore[attr-defined]
            if getattr(route, "path", "") == "/api"
        )

        assert api_app.version == __version__

    def test_package_version_has_one_source(self) -> None:
        """`pyproject.toml` is the one version source.

        A literal in `__init__.py` is a copy nothing forces to agree with
        it, and it had already drifted two major versions before this test
        existed. Deriving it from the installed distribution is what makes
        that impossible rather than merely unlikely.
        """
        from importlib.metadata import version

        from mail_verdict import __version__

        assert __version__ == version("mail-verdict")
