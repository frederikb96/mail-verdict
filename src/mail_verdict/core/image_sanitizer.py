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
    has_remote = bool(_REMOTE_IMG_RE.search(html)) or bool(_DATA_X_SRC_RE.search(html))
    stripped = _REMOTE_IMG_RE.sub("", html)
    stripped = _DATA_X_SRC_RE.sub("", stripped)
    return stripped, has_remote


def restore_remote_images(html: str) -> str:
    """
    Restore data-x-src attributes back to src for rendering with images allowed.

    Args:
        html: Sanitized email HTML with data-x-src attributes

    Returns:
        HTML with data-x-src converted back to src
    """
    return re.sub(
        r"\bdata-x-src\s*=",
        "src=",
        html,
        flags=re.IGNORECASE,
    )


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
