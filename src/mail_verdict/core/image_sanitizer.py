"""
Read-time image blocking for email privacy.

Strips remote <img> tags from HTML unless the sender or domain is
in the account's image exception allowlist. Inline images (data: URIs,
cid: references) are always preserved.

Separate from the store-time nh3 XSS sanitizer in sanitizer.py.
"""

from __future__ import annotations

import re

_REMOTE_IMG_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"'](?:https?://)[^\"']*[\"'][^>]*/?>",
    re.IGNORECASE,
)

_DATA_X_SRC_RE = re.compile(
    r"<img\b[^>]*?\bdata-x-src\s*=\s*[\"'][^\"']*[\"'][^>]*/?>",
    re.IGNORECASE,
)

# A style attribute whose remote url() was neutralised at sanitize time.
# Counted as a blocked image so the banner tells the truth: a tracking
# pixel in CSS is blocked for the same reason and needs the same consent.
_DATA_X_STYLE_RE = re.compile(
    r'\bdata-x-style\s*=\s*["\'][^"\']*["\']',
    re.IGNORECASE,
)


def strip_remote_images(html: str) -> tuple[str, bool]:
    """
    Remove all remote <img> tags from HTML.

    Preserves inline images with data: URIs and cid: references.
    Also detects data-x-src attributes (from store-time sanitizer)
    as indicators of blocked remote images.

    Args:
        html: Sanitized email HTML

    Returns:
        Tuple of (stripped HTML, whether any remote images were found)
    """
    has_remote = (
        bool(_REMOTE_IMG_RE.search(html))
        or bool(_DATA_X_SRC_RE.search(html))
        or bool(_DATA_X_STYLE_RE.search(html))
    )
    stripped = _REMOTE_IMG_RE.sub("", html)
    stripped = _DATA_X_SRC_RE.sub("", stripped)
    return stripped, has_remote


_SAFE_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def _restore_if_safe(match: re.Match[str]) -> str:
    """
    Restore data-x-src to src only if the URL uses http(s).

    Non-http schemes (javascript:, data:, vbscript:, etc.) are stripped
    entirely to prevent XSS.

    Args:
        match: Regex match for a data-x-src attribute with its value

    Returns:
        Restored src attribute or empty string if unsafe
    """
    url = match.group(1)
    if _SAFE_SCHEME_RE.match(url):
        return f'src="{url}"'
    return ""


def _restore_one_style(match: re.Match[str]) -> str:
    """Put a preserved style back, dropping any url() that is not http(s).

    The stored value is the sender's original CSS, so it gets the same
    scheme check the src path gets -- allowing a sender to load images is
    not consent to a javascript: or data: url reappearing in a rule.
    """
    original = match.group(1)
    safe = re.sub(
        r"url\(\s*['\"]?\s*([^'\")]+?)\s*['\"]?\s*\)",
        lambda m: m.group(0) if _SAFE_SCHEME_RE.match(m.group(1)) else "url(about:blank)",
        original,
        flags=re.IGNORECASE,
    )
    return f'style="{safe}"'


def _restore_styles(html: str) -> str:
    """Swap each neutralised style for its preserved original."""
    html = re.sub(
        r'\bstyle\s*=\s*["\'][^"\']*["\']\s+(?=data-x-style)', "", html, flags=re.IGNORECASE,
    )
    return re.sub(
        r'\bdata-x-style\s*=\s*["\']([^"\']*)["\']',
        _restore_one_style,
        html,
        flags=re.IGNORECASE,
    )


def restore_remote_images(html: str) -> str:
    """
    Restore data-x-src attributes back to src for rendering with images allowed.

    Only restores URLs with http:// or https:// schemes. Other schemes
    (javascript:, vbscript:, data:) are dropped to prevent XSS.

    Args:
        html: Sanitized email HTML with data-x-src attributes

    Returns:
        HTML with safe data-x-src converted back to src
    """
    html = re.sub(
        r'\bdata-x-src\s*=\s*["\']([^"\']*)["\']',
        _restore_if_safe,
        html,
        flags=re.IGNORECASE,
    )
    return _restore_styles(html)


def extract_sender_domain(email_addr: str | None) -> str | None:
    """
    Extract the domain from an email address.

    Args:
        email_addr: Email address string (may contain display name)

    Returns:
        Domain portion or None if not parseable
    """
    if not email_addr:
        return None
    # Handle "Name <email@domain.com>" format
    match = re.search(r"[\w.+-]+@([\w.-]+)", email_addr)
    if match:
        return match.group(1).lower()
    return None


def extract_sender_email(email_addr: str | None) -> str | None:
    """
    Extract the bare email address from a from_addr string.

    Args:
        email_addr: Email address string (may contain display name)

    Returns:
        Bare email address or None if not parseable
    """
    if not email_addr:
        return None
    match = re.search(r"([\w.+-]+@[\w.-]+)", email_addr)
    if match:
        return match.group(1).lower()
    return None
