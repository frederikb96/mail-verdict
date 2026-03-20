"""
HTML email sanitization using nh3.

Whitelist approach: only safe tags/attributes pass through.
Remote images rewritten to data-x-src for privacy (SnappyMail pattern).
"""

from __future__ import annotations

import re

import nh3

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "div",
    "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "img", "ins", "li", "ol", "p", "pre", "q", "s", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "u", "ul", "center", "font",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"src", "data-x-src", "alt", "width", "height", "title"},
    "td": {"colspan", "rowspan", "align", "valign", "width", "data-x-bg"},
    "th": {"colspan", "rowspan", "align", "valign", "width"},
    "table": {"border", "cellpadding", "cellspacing", "width", "align"},
    "font": {"color", "size", "face"},
    "div": {"align"},
    "p": {"align"},
    "*": {"class", "style", "dir", "lang"},
}

_SRC_RE = re.compile(r'\bsrc\s*=\s*"([^"]*)"', re.IGNORECASE)
_SRC_SINGLE_RE = re.compile(r"\bsrc\s*=\s*'([^']*)'", re.IGNORECASE)
_BG_RE = re.compile(r'\bbackground\s*=\s*"([^"]*)"', re.IGNORECASE)


def _rewrite_src(match: re.Match[str]) -> str:
    """Replace src with data-x-src, preserving CID references."""
    url = match.group(1)
    if url.lower().startswith("cid:"):
        return match.group(0)
    return f'data-x-src="{url}"'


def _rewrite_src_single(match: re.Match[str]) -> str:
    """Replace single-quoted src with data-x-src, preserving CID references."""
    url = match.group(1)
    if url.lower().startswith("cid:"):
        return match.group(0)
    return f"data-x-src='{url}'"


def _rewrite_bg(match: re.Match[str]) -> str:
    """Replace background attribute with data-x-bg."""
    url = match.group(1)
    return f'data-x-bg="{url}"'


def _rewrite_remote_images(html: str) -> str:
    """
    Replace img src and background URLs with data-x-* attributes.

    CID references (inline MIME images) are preserved as-is.

    Args:
        html: Raw email HTML

    Returns:
        HTML with remote images blocked
    """
    html = _SRC_RE.sub(_rewrite_src, html)
    html = _SRC_SINGLE_RE.sub(_rewrite_src_single, html)
    html = _BG_RE.sub(_rewrite_bg, html)
    return html


def sanitize_email_html(html: str) -> str:
    """
    Sanitize email HTML for safe rendering.

    Two-step process:
    1. Rewrite remote image URLs to data-x-src (blocks loading)
    2. Sanitize with nh3 whitelist (strips dangerous elements)

    Args:
        html: Raw email HTML from database

    Returns:
        Sanitized HTML safe for rendering in Shadow DOM
    """
    html = _rewrite_remote_images(html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https", "mailto", "cid"},
    )
