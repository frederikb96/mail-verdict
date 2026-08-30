"""
Security response headers, applied to every response.

A pure ASGI middleware rather than Starlette's BaseHTTPMiddleware:
BaseHTTPMiddleware buffers the response body through `call_next`, which has
a documented history of breaking a long-lived streaming response -- and
`/api/events` is exactly that. Headers only touch the `http.response.start`
message here, so the body that follows, streamed or not, passes through
untouched.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)

# Swagger UI and ReDoc load their own assets from a CDN and are FastAPI's
# own developer-facing pages, not part of the application surface the rest
# of this policy is written for.
_CSP_EXEMPT_PREFIXES = ("/api/docs", "/api/redoc")

_STATIC_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    # Ignored by a browser that received it over plain HTTP, so this is a
    # no-op in development and only takes effect once actually served over
    # TLS.
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
]


def compute_inline_script_hashes(build_dir: Path) -> frozenset[str]:
    """CSP script-src hashes for every inline <script> the built UI ships.

    Next.js's static export embeds its React hydration payload as inline
    scripts generated fresh on every build, so a fixed list pasted into
    source would need hand-editing on every UI change and silently drift
    the moment someone forgot. Reading it from the actual build output
    instead means the policy can never allow a script this build did not
    produce, and never disallow one that it did.

    Args:
        build_dir: The built UI's output directory (may not exist yet).

    Returns:
        One 'sha256-...' CSP source per distinct inline script found.
    """
    hashes: set[str] = set()
    if not build_dir.exists():
        return frozenset(hashes)
    for html_file in build_dir.rglob("*.html"):
        text = html_file.read_text(encoding="utf-8")
        for match in _INLINE_SCRIPT_RE.finditer(text):
            digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
            hashes.add(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return frozenset(hashes)


def build_content_security_policy(script_hashes: frozenset[str]) -> str:
    """The Content-Security-Policy value every non-exempt response carries.

    style-src allows inline styles because the application's whole purpose
    is rendering sender-authored `style="..."` attributes on email content
    inside a shadow root -- CSS injection there is a much lower-severity
    concern than script execution, and it is the sanitizer plus the shadow
    host's own layout containment that actually keep a message's CSS
    inside its own box (see sanitizer.py and email-renderer.tsx), not this
    policy.

    img-src allows any http(s) origin because a message's own image-consent
    system, not this policy, is what decides whether a remote image loads
    at all -- once a sender is allowed, their image host is whatever they
    chose.

    Args:
        script_hashes: CSP sources for the UI's own inline scripts, from
            compute_inline_script_hashes.

    Returns:
        A single semicolon-joined policy string.
    """
    script_src = " ".join(["'self'", *sorted(script_hashes)])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: http: https:",
            "object-src 'none'",
            "frame-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ]
    )


class SecurityHeadersMiddleware:
    """Adds a fixed set of response headers, CSP included, to every response."""

    def __init__(self, app: ASGIApp, content_security_policy: str) -> None:
        """
        Args:
            app: The wrapped ASGI application.
            content_security_policy: The value from build_content_security_policy.
        """
        self.app = app
        self._csp = content_security_policy.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Inject headers into `http.response.start`; pass everything else through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        exempt_csp = scope["path"].startswith(_CSP_EXEMPT_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_STATIC_HEADERS)
                if not exempt_csp:
                    headers.append((b"content-security-policy", self._csp))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
