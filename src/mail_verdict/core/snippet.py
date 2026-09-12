"""The list row's preview text -- the first characters of a message's
plain-text body, with boilerplate lines skipped first.

Without this, a reply's own quoted forward header ("________________________
Von: ...") or a newsletter's tracking link ("View this email in your
browser (https://...)") is what a reader sees in the list, rather than the
one line they would actually want. Every caller building a MessageSummary,
MessageDetail or SpamReviewItem snippet from a message's own body_text goes
through here, so the rule has one definition.
"""

import re

_SNIPPET_LIMIT = 120

# A run of at least three of the same rule character -- "___", "---",
# "===" -- however long, however it is spaced ("- - -" is still visually a
# separator but is left alone here; sender tooling almost always emits an
# unbroken run).
_SEPARATOR_LINE_RE = re.compile(r"^[_\-=]{3,}$")

# A forwarded or quoted message's own header block, German and English
# alike -- the labels a mail client actually writes before an inline
# forward, not a generic "starts with a word and a colon" guess.
_QUOTED_HEADER_RE = re.compile(
    r"^(Von|From|Gesendet|Sent|An|To|Cc|Betreff|Subject)\s*:", re.IGNORECASE,
)

# "View this email in your browser", "View this post on the web", "Im
# Browser lesen/ansehen/öffnen" -- the boilerplate line every newsletter and
# mailing-list platform prepends, always paired with a tracking link.
_VIEW_ONLINE_RE = re.compile(
    r"view (this|it)?\s*(email|post|message)?\s*(in|on)\s*(your |the )?(browser|web)"
    r"|im\s+browser\s+(lesen|ansehen|öffnen)",
    re.IGNORECASE,
)

# A line that is nothing but a URL, parentheses optionally wrapped around
# it the way "(https://...)" often is inline after a boilerplate phrase.
_BARE_URL_LINE_RE = re.compile(r"^\(?https?://\S+\)?$", re.IGNORECASE)


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False  # blank lines are collapsed below, not filtered here
    return bool(
        _SEPARATOR_LINE_RE.match(stripped)
        or _QUOTED_HEADER_RE.match(stripped)
        or _VIEW_ONLINE_RE.search(stripped)
        or _BARE_URL_LINE_RE.match(stripped),
    )


def build_snippet(body_text: str | None, limit: int = _SNIPPET_LIMIT) -> str | None:
    """The list row's preview: `body_text` with noise lines removed, then
    truncated to `limit` characters. `None` for an empty body or one that
    is noise from end to end (a bare forwarded header with nothing else)."""
    if not body_text:
        return None
    kept = [line for line in body_text.splitlines() if not _is_noise_line(line)]
    # Collapsed to single spaces for a one-line preview -- several short
    # kept lines read better joined than with embedded newlines truncated
    # mid-sentence by the character limit below.
    collapsed = " ".join(" ".join(kept).split())
    return collapsed[:limit] or None
