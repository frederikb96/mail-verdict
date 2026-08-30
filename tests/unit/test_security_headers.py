"""Tests for the security response headers: CSP hash derivation and the
header-injecting middleware itself.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from mail_verdict.api.security_headers import (
    SecurityHeadersMiddleware,
    build_content_security_policy,
    compute_inline_script_hashes,
)


class TestComputeInlineScriptHashes:
    """CSP script-src sources derived from the actual built HTML."""

    def test_an_inline_script_produces_a_hash(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(
            "<html><body><script>console.log('hi')</script></body></html>"
        )
        hashes = compute_inline_script_hashes(tmp_path)
        assert len(hashes) == 1
        assert next(iter(hashes)).startswith("'sha256-")

    def test_an_external_script_produces_no_hash(self, tmp_path: Path) -> None:
        """A <script src="..."> needs no hash -- script-src 'self' already covers it."""
        (tmp_path / "index.html").write_text(
            '<html><body><script src="/app.js"></script></body></html>'
        )
        assert compute_inline_script_hashes(tmp_path) == frozenset()

    def test_different_script_content_produces_a_different_hash(self, tmp_path: Path) -> None:
        """The hash has to change with the content, or it is not checking anything."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "a.html").write_text("<script>one()</script>")
        (dir_b / "b.html").write_text("<script>two()</script>")
        assert compute_inline_script_hashes(dir_a) != compute_inline_script_hashes(dir_b)

    def test_a_missing_build_directory_yields_no_hashes(self, tmp_path: Path) -> None:
        """The UI may not be built yet -- must not raise."""
        assert compute_inline_script_hashes(tmp_path / "does-not-exist") == frozenset()

    def test_scripts_across_multiple_pages_are_all_collected(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<script>indexPage()</script>")
        nested = tmp_path / "settings"
        nested.mkdir()
        (nested / "index.html").write_text("<script>settingsPage()</script>")
        assert len(compute_inline_script_hashes(tmp_path)) == 2


class TestBuildContentSecurityPolicy:
    """The policy string itself."""

    def test_script_hashes_are_included(self) -> None:
        csp = build_content_security_policy(frozenset({"'sha256-abc123'"}))
        assert "'sha256-abc123'" in csp

    def test_frame_ancestors_is_none(self) -> None:
        """default-src does not cover frame-ancestors -- it must be explicit."""
        csp = build_content_security_policy(frozenset())
        assert "frame-ancestors 'none'" in csp

    def test_style_src_allows_inline_for_email_rendering(self) -> None:
        csp = build_content_security_policy(frozenset())
        assert "style-src 'self' 'unsafe-inline'" in csp

    def test_script_src_has_no_unsafe_inline(self) -> None:
        """The one directive this policy exists to keep strict."""
        csp = build_content_security_policy(frozenset())
        script_src = next(part for part in csp.split("; ") if part.startswith("script-src"))
        assert "unsafe-inline" not in script_src


class TestSecurityHeadersMiddleware:
    """The ASGI middleware wiring."""

    def _app(self) -> ASGIApp:
        async def ok(request: object) -> PlainTextResponse:  # noqa: ARG001
            return PlainTextResponse("ok")

        async def streaming(request: object) -> StreamingResponse:  # noqa: ARG001
            async def body() -> object:
                yield b"chunk-1"
                yield b"chunk-2"

            return StreamingResponse(body())

        app = Starlette(
            routes=[
                Route("/plain", ok),
                Route("/api/docs", ok),
                Route("/stream", streaming),
            ]
        )
        return SecurityHeadersMiddleware(app, content_security_policy="default-src 'self'")

    def test_ordinary_response_carries_every_header(self) -> None:
        client = TestClient(self._app())
        resp = client.get("/plain")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["content-security-policy"] == "default-src 'self'"
        assert "strict-transport-security" in resp.headers

    def test_docs_path_is_exempt_from_csp_but_not_other_headers(self) -> None:
        """Swagger UI needs its CDN scripts; it does not need to lose nosniff too."""
        client = TestClient(self._app())
        resp = client.get("/api/docs")
        assert "content-security-policy" not in resp.headers
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_a_streaming_response_is_not_buffered_or_altered(self) -> None:
        """/api/events is exactly this shape -- the reason for a pure ASGI
        middleware instead of BaseHTTPMiddleware in the first place."""
        client = TestClient(self._app())
        resp = client.get("/stream")
        assert resp.text == "chunk-1chunk-2"
        assert resp.headers["content-security-policy"] == "default-src 'self'"

class TestCspExemptionMatchesWholePaths:
    """A path that merely starts with a documentation route must keep its policy.

    Prefix matching hands a future `/api/docs-export` the exemption written
    for Swagger UI, and it loses its CSP with nothing saying so.
    """

    def test_a_route_beginning_with_an_exempt_path_is_not_exempt(self) -> None:
        from mail_verdict.api.security_headers import _is_csp_exempt

        assert _is_csp_exempt("/api/docs")
        assert _is_csp_exempt("/api/redoc")
        assert not _is_csp_exempt("/api/docs-export")
        assert not _is_csp_exempt("/api/redocument")
