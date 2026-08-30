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
    # background is allowed through the sanitizer only so the rewrite below
    # can turn it into data-x-bg. Stripping it outright would block the
    # remote fetch too, but would also lose it permanently -- an allowlisted
    # sender could never get their background back.
    "td": {"colspan", "rowspan", "align", "valign", "width", "background", "data-x-bg"},
    "th": {"colspan", "rowspan", "align", "valign", "width", "background", "data-x-bg"},
    "table": {
        "border", "cellpadding", "cellspacing", "width", "align",
        "background", "data-x-bg",
    },
    "font": {"color", "size", "face"},
    "div": {"align"},
    "p": {"align"},
    "*": {"class", "style", "data-x-style", "dir", "lang"},
}

_SRC_RE = re.compile(r'\bsrc\s*=\s*"([^"]*)"', re.IGNORECASE)
_SRC_SINGLE_RE = re.compile(r"\bsrc\s*=\s*'([^']*)'", re.IGNORECASE)
_BG_RE = re.compile(r'\bbackground\s*=\s*"([^"]*)"', re.IGNORECASE)

# A style attribute can fetch a remote resource through any of several CSS
# properties -- background-image, background, list-style-image, border-image,
# content, cursor and more. Matching url() itself rather than the property
# names is what keeps this from being a list that a new property defeats.
_STYLE_RE = re.compile(r'\bstyle\s*=\s*"([^"]*)"', re.IGNORECASE)
_STYLE_SINGLE_RE = re.compile(r"\bstyle\s*=\s*'([^']*)'", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?\s*([^'\")]+?)\s*['\"]?\s*\)", re.IGNORECASE)

_LOCAL_URL_PREFIXES = ("cid:", "data:", "about:", "#")


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


def _is_remote(url: str) -> bool:
    """Whether fetching this URL would reach the network."""
    return not url.strip().lower().startswith(_LOCAL_URL_PREFIXES)


def _rewrite_style(match: re.Match[str], quote: str) -> str:
    """Neutralise remote url() in a style attribute, keeping the original.

    The neutralised declaration stays in place rather than the whole
    attribute being moved aside, so the element keeps its layout while its
    remote fetch is dead. The untouched original goes to data-x-style, which
    is what the image-policy layer restores from once a sender is allowed.
    """
    style = match.group(1)
    if not any(_is_remote(url) for url in _CSS_URL_RE.findall(style)):
        return match.group(0)

    blocked = _CSS_URL_RE.sub(
        lambda m: m.group(0) if not _is_remote(m.group(1)) else "url(about:blank)",
        style,
    )
    return f"style={quote}{blocked}{quote} data-x-style={quote}{style}{quote}"


def _rewrite_remote_images(html: str) -> str:
    """
    Replace img src, background and CSS url() references with data-x-*.

    CID references (inline MIME images) are preserved as-is.

    Args:
        html: Raw email HTML

    Returns:
        HTML with remote images blocked
    """
    html = _SRC_RE.sub(_rewrite_src, html)
    html = _SRC_SINGLE_RE.sub(_rewrite_src_single, html)
    html = _BG_RE.sub(_rewrite_bg, html)
    html = _STYLE_RE.sub(lambda m: _rewrite_style(m, '"'), html)
    html = _STYLE_SINGLE_RE.sub(lambda m: _rewrite_style(m, "'"), html)
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
    # Sanitize FIRST, then rewrite. Matching attributes in raw email HTML
    # means matching however the sender chose to write them, and an
    # unquoted attribute slips a pattern that expects quotes -- which is a
    # silent hole, since the rewrite simply does not fire. nh3 normalises
    # every attribute to a quoted form, so rewriting its output matches
    # one shape rather than every shape a sender might produce.
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https", "mailto", "cid"},
    )
    return _rewrite_remote_images(cleaned)
