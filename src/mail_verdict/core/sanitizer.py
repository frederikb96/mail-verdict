"""
HTML email sanitization using nh3.

Whitelist approach: only safe tags/attributes pass through.
Remote images rewritten to data-x-src for privacy (SnappyMail pattern).
"""

from __future__ import annotations

import re

import nh3
import tinycss2
from tinycss2.ast import Declaration, Node

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

_LOCAL_URL_PREFIXES = ("cid:", "data:", "about:", "#")

# Positioning, stacking and transforms are how content leaves the box it was
# rendered into. Nothing in an email needs them. Compared against a name
# tinycss2 has already parsed, so a vendor variant such as -webkit-transform
# is caught under the unprefixed name it is a variant of (see
# _canonical_property_name below) rather than needing its own entry.
_ESCAPING_PROPERTIES = frozenset({
    "position", "z-index",
    "top", "right", "bottom", "left",
    "inset", "inset-block", "inset-block-start", "inset-block-end",
    "inset-inline", "inset-inline-start", "inset-inline-end",
    "transform", "transform-origin", "transform-style", "transform-box",
    "translate", "rotate", "scale",
    "perspective", "perspective-origin",
})

_VENDOR_PREFIX_RE = re.compile(r"^-[a-z]+-")


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


def _canonical_property_name(name: str) -> str:
    """Fold a vendor-prefixed property onto the unprefixed name it varies.

    ``-webkit-transform`` is enforced exactly like ``transform`` -- browsers
    honour the prefixed form, so a check keyed on the bare name alone misses
    it.
    """
    return _VENDOR_PREFIX_RE.sub("", name)


def _parsed_declarations(style: str) -> list[Declaration]:
    """Parse a style value, dropping the declarations that escape the box.

    A shadow root isolates styles but does not create a containing block, so
    position:fixed is resolved against the viewport and the message can cover
    the whole application. Wrapped in a link, every click anywhere then
    belongs to the sender. Stacking and transforms reach the same end by
    other routes.

    The property name is compared only after a real CSS tokenizer has
    resolved it. Splitting the text on ``:`` is not how CSS is written: a
    comment between the name and the colon (``top/**/:0``) or a hex escape
    inside the name (``p\\6fsition:fixed``) both parse as ordinary
    declarations in every browser and slip straight past a string
    comparison. tinycss2 is the same class of tokenizer a browser uses, so
    comments and escapes are resolved before any name is compared, which
    closes the class rather than the instance.

    Message layout does not need any of the escaping declarations, so they
    are dropped rather than inspected -- a value allowlist is a longer list
    to keep correct and buys nothing here. Anything that fails to parse as
    an ordinary declaration carries no layout value an email needs either,
    and is dropped along with it.
    """
    kept = []
    for node in tinycss2.parse_declaration_list(
        style, skip_comments=True, skip_whitespace=True
    ):
        if node.type != "declaration":
            continue
        if _canonical_property_name(node.lower_name) in _ESCAPING_PROPERTIES:
            continue
        kept.append(node)
    return kept


def _serialize_declarations(declarations: list[Declaration]) -> str:
    """Render parsed declarations back into a style attribute value."""
    parts = []
    for decl in declarations:
        value = tinycss2.serialize(decl.value).strip()
        important = " !important" if decl.important else ""
        parts.append(f"{decl.lower_name}:{value}{important}")
    return "; ".join(parts)


def _find_remote_url(nodes: list[Node]) -> bool:
    """Whether a url() reaching the network appears anywhere in this value.

    Walks the parsed token tree -- including inside a nested function such
    as image-set() -- rather than matching ``url(`` as literal text, so a
    reference hidden behind a comment or an escaped function name
    (``ur\\6c(...)``) is found exactly like an ordinary one; the tokenizer
    has already resolved both by this point.
    """
    for node in nodes:
        if node.type == "url":
            if _is_remote(node.value):
                return True
        elif node.type == "function":
            if node.lower_name == "url" and any(
                arg.type == "string" and _is_remote(arg.value) for arg in node.arguments
            ):
                return True
            if _find_remote_url(node.arguments):
                return True
        elif node.type in ("() block", "[] block", "{} block"):
            if _find_remote_url(node.content):
                return True
    return False


def _neutralize_remote_urls(nodes: list[Node]) -> None:
    """Replace every url() reaching the network with url(about:blank), in place."""
    for node in nodes:
        if node.type == "url":
            if _is_remote(node.value):
                node.value = "about:blank"
                node.representation = "url(about:blank)"
        elif node.type == "function":
            if node.lower_name == "url":
                for arg in node.arguments:
                    if arg.type == "string" and _is_remote(arg.value):
                        arg.value = "about:blank"
                        arg.representation = '"about:blank"'
            _neutralize_remote_urls(node.arguments)
        elif node.type in ("() block", "[] block", "{} block"):
            _neutralize_remote_urls(node.content)


def _rewrite_style(match: re.Match[str], quote: str) -> str:
    """Make one style attribute safe, keeping the original for restoration.

    Two separate concerns. Declarations that let content escape its box are
    dropped outright and never come back. A remote url() is only neutralised
    -- the declaration stays in place so layout survives, and the original
    goes to data-x-style, which the image-policy layer restores from once a
    sender is allowed.
    """
    style = match.group(1)
    declarations = _parsed_declarations(style)
    preserved = _serialize_declarations(declarations)
    has_remote = any(_find_remote_url(decl.value) for decl in declarations)

    if not has_remote:
        if preserved == style:
            return match.group(0)
        return f"style={quote}{preserved}{quote}"

    # Re-parsed from the already-cleaned text so the mutation below leaves
    # `declarations` -- and the `preserved` string built from it -- alone.
    blocked_declarations = [
        node
        for node in tinycss2.parse_declaration_list(
            preserved, skip_comments=True, skip_whitespace=True
        )
        if node.type == "declaration"
    ]
    for decl in blocked_declarations:
        _neutralize_remote_urls(decl.value)
    blocked = _serialize_declarations(blocked_declarations)
    return f"style={quote}{blocked}{quote} data-x-style={quote}{preserved}{quote}"


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
