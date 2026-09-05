"""
Outbound HTML sanitisation -- the boundary that makes browser-composed,
pasted and quoted content safe to send as mail, applied once for every
producer of outbox.body_html: the compose API before insert_outbox(), and
this module's sibling quote endpoint in api/mails.py.

core/sanitizer.py (the inbound display sanitiser) keeps a message's own
styling intact behind an isolated Shadow DOM, because the message stays on
our own page. Outbound content leaves for other people's mail clients with
no isolation at all, so the allowlist here is much smaller: no class or
style attribute survives from the input, and the handful of declarations
mail actually needs -- the quote bar, a monospace code block, unwrapped
list items -- are set by this pass itself rather than passed through. The
one exception is the two fixed class values the compose editor's own
quote wrapper writes (gmail_quote, gmail_attr) -- the shape Gmail keys its
quote-collapsing UI on -- allowed by exact value rather than by name.
Exact-value matching stops a quoted message's own content from combining
one of those values onto an attribute that already carries something
else; it does not stop a sender simply writing `class="gmail_quote"`
outright, which passes unchanged. That is harmless today only because the
editor's node view (quoted-message-node.ts) takes the first blockquote
and the first `.gmail_attr` in document order, and the wrapper's own
markup is always written first -- a forged one from a quoted sender can
only ever land after it.
"""

from __future__ import annotations

import re

import nh3

ALLOWED_TAGS = {
    "p", "br", "strong", "em", "u", "s", "a",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "blockquote", "pre", "code", "hr",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "img", "div",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href"},
    "blockquote": {"type"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

# nh3 matches these against an attribute's whole value, not per token, so
# anything other than exactly this string is dropped -- the quote wrapper
# never combines them with anything else, which is what makes an
# exact-value allowlist the right tool rather than a token filter.
# data-quoted-message="true" is what the compose editor's own quotedMessage
# node (ui/src/components/mail/editor/quoted-message-node.ts) matches on
# to reconstruct itself when a saved draft is reopened -- without it here,
# every draft carrying a quote would lose that marker on its first save
# and never round-trip back into the node again.
ALLOWED_TAG_ATTRIBUTE_VALUES: dict[str, dict[str, set[str]]] = {
    "div": {
        "class": {"gmail_quote", "gmail_attr"},
        "data-quoted-message": {"true"},
    },
    "blockquote": {"class": {"gmail_quote"}},
}

# Gmail's own quote-bar values -- the shape every client recognises and
# renders, rather than an indent that only some of them apply on their own.
_BLOCKQUOTE_STYLE = "margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex"
_PRE_STYLE = "font-family:monospace;background:#f4f4f5;padding:8px 12px;border-radius:4px"

_BLOCKQUOTE_OPEN_RE = re.compile(r"<blockquote([^>]*)>")
_PRE_OPEN_RE = re.compile(r"<pre([^>]*)>")
# ProseMirror's own list serialisation wraps each item's direct text in a
# paragraph, so a plain string substitution is safe here rather than a full
# HTML parse. Word and Gmail both apply their own per-<p> margin, which
# otherwise renders as a blank line per bullet.
_LIST_ITEM_PARAGRAPH_RE = re.compile(r"<li><p>(.*?)</p>", re.DOTALL)
# nh3 strips a disallowed src (a cid: reference, or a local /api/... URL
# with nothing on the other end for a recipient) rather than the whole
# tag, leaving a bare <img> with nothing to show. There is nothing to
# attach such an image to, so the tag itself goes too.
_IMG_NO_SRC_RE = re.compile(r"<img(?![^>]*\bsrc=)[^>]*>")


def sanitize_outbound_html(html: str) -> str:
    """
    Make browser-composed, pasted or quoted HTML safe to send as mail.

    Three things happen in order: an nh3 allowlist strips everything
    outside the small vocabulary above (no class, no style, no script, no
    positioning); list items lose the paragraph wrapper their own
    serialisation adds; and the two declarations mail clients need but
    accept no class or stylesheet for -- the quote bar, a monospace code
    block -- are set directly, never read from the input, since nh3 has
    already dropped whatever style attribute it carried.
    """
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        tag_attribute_values=ALLOWED_TAG_ATTRIBUTE_VALUES,
        # rel/target are a browser concern; mail has no tab to open one
        # in, so nh3's default of injecting them is switched off rather
        # than allowlisted away tag by tag.
        link_rel=None,
        url_schemes={"http", "https", "mailto"},
        # A relative URL has no scheme for url_schemes to judge at all, so
        # without this a message's own /api/... attachment URL -- which
        # means nothing outside this application -- would otherwise pass
        # straight through.
        url_relative="deny",
    )
    cleaned = _LIST_ITEM_PARAGRAPH_RE.sub(r"<li>\1", cleaned)
    cleaned = _IMG_NO_SRC_RE.sub("", cleaned)
    cleaned = _BLOCKQUOTE_OPEN_RE.sub(
        lambda m: f'<blockquote{m.group(1)} style="{_BLOCKQUOTE_STYLE}">', cleaned,
    )
    cleaned = _PRE_OPEN_RE.sub(lambda m: f'<pre{m.group(1)} style="{_PRE_STYLE}">', cleaned)
    return cleaned
