"""
HTML email sanitization using nh3.

Whitelist approach: only safe tags/attributes pass through.
Remote images rewritten to data-x-src for privacy (SnappyMail pattern).
"""

from __future__ import annotations

import html
import re

import nh3
import tinycss2
from tinycss2.ast import Declaration, Node

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "div",
    "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "img", "ins", "li", "ol", "p", "pre", "q", "s", "span", "strong",
    "style", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "u", "ul", "center", "font",
    # Plain structural/semantic tags with no attribute or behaviour this
    # module treats specially. None of them can do anything past what an
    # ordinary inline wrapper already can.
    #
    # This set and the client's own DOMPurify allowlist in
    # email-renderer.tsx answer one question in two places, so the stricter
    # of the two silently wins: a tag this list drops never reaches the
    # client to be judged, and one the client drops is invisible however
    # permissive this list is. test_sanitizer.py compares them.
    "figure", "figcaption", "details", "summary", "small", "mark",
    "section", "article", "header", "footer", "nav", "cite", "caption",
    "col", "colgroup", "kbd", "wbr", "address", "dfn", "main",
}

# A tag outside ALLOWED_TAGS has its own tag stripped, but by default only
# `script` also loses its text content -- every other disallowed tag is
# unwrapped, hoisting its text into the surrounding body. That is correct
# for a stray inline wrapper (a `<blink>` or a custom element still ought
# to show its text), but wrong for a document-structure tag: a `<title>`
# or `<head>` was never meant to be visible copy, and an ESP-generated
# message routinely carries several kilobytes of exactly this kind of
# markup ahead of the actual content, which is why it surfaces at the very
# top of the rendered message. These lose their content along with the
# tag; nh3's own default (script) is named explicitly too, so the set does
# not depend on it not changing under us.
#
# `style` is deliberately absent from this set even though its content is
# handled specially below: it is a real tag with real content a message
# needs, sanitised rather than discarded -- see _sanitize_stylesheet.
CONTENT_STRIPPED_TAGS = {
    "script", "title", "head", "meta", "xml", "noscript",
    "iframe", "audio", "video", "object", "embed",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "width", "height", "title"},
    # type="cite" is how nearly every mail client marks a reply's own
    # quoted original -- purely informational, so allowing it through
    # costs nothing, and it is the one signal the reading pane's own
    # quote-collapsing can rely on across senders that omit a class name.
    "blockquote": {"type"},
    # background is allowed through the sanitizer only so the rewrite below
    # can turn it into data-x-bg. Stripping it outright would block the
    # remote fetch too, but would also lose it permanently -- an allowlisted
    # sender could never get their background back.
    "td": {"colspan", "rowspan", "align", "valign", "width", "background"},
    "th": {"colspan", "rowspan", "align", "valign", "width", "background"},
    "table": {"border", "cellpadding", "cellspacing", "width", "align", "background"},
    "font": {"color", "size", "face"},
    "div": {"align"},
    "p": {"align"},
    # media scopes a stylesheet the same way a media query inside it would
    # -- kept for the ESPs that write `<style media="(prefers-color-scheme:
    # dark)">` rather than wrapping the whole block in an @media rule.
    "style": {"media"},
    # An id cannot collide with anything of this application's own inside
    # an isolated shadow root, so a long newsletter's own table of
    # contents (an in-page #fragment link to one of its own headings) can
    # actually work -- see email-renderer.tsx's click handler for the
    # other half of this.
    "*": {"class", "style", "dir", "lang", "id"},
}

# data-x-src, data-x-bg, data-x-style and data-x-stylesheet are this
# application's own protocol for a neutralised remote reference, never a
# sender's -- they are added by rewrite_remote_images below, which runs
# strictly *after* nh3.clean, so keeping them out of ALLOWED_ATTRIBUTES
# loses nothing a sender could legitimately write. It closes a sender
# writing one of these names directly: nh3 would otherwise keep it
# unfiltered, since none of the regexes above -- which only ever match a
# plain `style=`/`src=`/`background=` -- run against an attribute already
# named data-x-*, and the restoration path in image_sanitizer.py splices
# whatever it finds there back into the page as markup or raw <style>
# content. See refilter_style_declarations and refilter_stylesheet_css
# below for the second half of the fix: restoring re-runs the same
# filter this module already applies at store time, rather than trusting
# that a stored value was necessarily produced by it.

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

# CSS functions besides url() whose string argument can also name a remote
# resource -- src() (a proposed general-purpose alternative to url()) and
# image() (CSS Images level 4) both take a plain string, so a check keyed
# on url tokens alone misses them entirely.
_URL_STRING_FUNCTIONS = frozenset({"url", "src", "image"})

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
})

# transform/translate/rotate/scale/perspective are deliberately not in
# _ESCAPING_PROPERTIES above: with :host no longer sender-writable (see
# _selector_escapes_containment), `contain: layout paint` is a real
# containing block a transform cannot resolve outside of, exactly like an
# ordinary margin cannot -- dropping them bought no protection past what
# containment already provides, at the cost of rendering fidelity for
# ordinary modern mail. position stays fully dropped except for one
# value -- see _is_allowed_sticky_position -- since fixed/absolute
# genuinely change what box the content resolves against.

_VENDOR_PREFIX_RE = re.compile(r"^-[a-z]+-")


def _rewrite_src(match: re.Match[str]) -> str:
    """Replace src with data-x-src, preserving local references."""
    url = match.group(1)
    if not _is_remote(url):
        return match.group(0)
    return f'data-x-src="{url}"'


def _rewrite_src_single(match: re.Match[str]) -> str:
    """Replace single-quoted src with data-x-src, preserving local references."""
    url = match.group(1)
    if not _is_remote(url):
        return match.group(0)
    return f"data-x-src='{url}'"


def _rewrite_bg(match: re.Match[str]) -> str:
    """Replace background attribute with data-x-bg, preserving local references."""
    url = match.group(1)
    if not _is_remote(url):
        return match.group(0)
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


def _filter_declarations(nodes: list[Node]) -> list[Declaration]:
    """Drop the declarations that escape the box, from an already-parsed list.

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

    Message layout does not need most of what an escaping property's value
    could be, so the property is dropped by name rather than inspected --
    position is the one exception, since sticky needs the name kept but
    is not itself escaping (see _is_allowed_position_value). Anything that
    fails to parse as an ordinary declaration carries no layout value an
    email needs either, and is dropped along with it.

    Shared by an inline style attribute and a message's own stylesheet --
    _parsed_declarations parses text into this shape, and a qualified rule
    or an @font-face block inside a <style> tag already comes tokenized this
    way, so both call this rather than each carrying their own copy of the
    filter.
    """
    kept = []
    for node in nodes:
        if node.type != "declaration":
            continue
        name = _canonical_property_name(node.lower_name)
        if name in _ESCAPING_PROPERTIES and not _is_allowed_position_value(name, node.value):
            continue
        if _contains_parse_error(node.value):
            continue
        kept.append(node)
    return kept


# sticky is the one position value _ESCAPING_PROPERTIES's blanket "position"
# entry would otherwise drop along with fixed/absolute -- it is clipped by
# the same overflow/containment as ordinary flow content and cannot escape
# the box a fixed or absolute value can, so it is allowed by value rather
# than by carving position out of the escaping-properties list entirely.
_ALLOWED_POSITION_VALUES = frozenset({"sticky", "-webkit-sticky"})


def _is_allowed_position_value(name: str, value: list[Node]) -> bool:
    """Whether an otherwise-escaping declaration's value is the one
    exception _ESCAPING_PROPERTIES carries for its own name."""
    if name != "position":
        return False
    tokens = [t for t in value if t.type not in ("whitespace", "comment")]
    return (
        len(tokens) == 1
        and tokens[0].type == "ident"
        and tokens[0].lower_value in _ALLOWED_POSITION_VALUES
    )


def _parsed_declarations(style: str) -> list[Declaration]:
    """Parse a style attribute's value and filter it -- see _filter_declarations."""
    return _filter_declarations(
        tinycss2.parse_declaration_list(style, skip_comments=True, skip_whitespace=True)
    )


def _contains_parse_error(nodes: list[Node]) -> bool:
    """Whether any token in this value failed to parse.

    A tokenizer recovering from malformed input echoes the malformed text
    back, so serializing such a value reproduces it verbatim -- an
    unterminated string comes back carrying the quote that opened it and
    nothing that closes it. A declaration that did not parse cleanly
    carries no layout an email needs, so it is dropped rather than
    round-tripped.
    """
    for node in nodes:
        if node.type == "error":
            return True
        if node.type == "function" and _contains_parse_error(node.arguments):
            return True
        if node.type in ("() block", "[] block", "{} block") and _contains_parse_error(
            node.content
        ):
            return True
    return False


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
            if node.lower_name in _URL_STRING_FUNCTIONS and any(
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
            if node.lower_name in _URL_STRING_FUNCTIONS:
                for arg in node.arguments:
                    if arg.type == "string" and _is_remote(arg.value):
                        arg.value = "about:blank"
                        arg.representation = '"about:blank"'
            _neutralize_remote_urls(node.arguments)
        elif node.type in ("() block", "[] block", "{} block"):
            _neutralize_remote_urls(node.content)


def _attr_value(text: str, quote: str) -> str:
    """Escape a string for use inside an HTML attribute delimited by `quote`.

    Whatever a CSS tokenizer hands back is being spliced into markup, so it
    has to be escaped there rather than trusted to contain no delimiter. An
    unescaped quote ends the attribute early and every character after it
    becomes markup the tag allowlist never approved -- an `onerror=` among
    them, on an element that already passed sanitization.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        return escaped.replace('"', "&quot;")
    return escaped.replace("'", "&#39;")


def _rewrite_style(match: re.Match[str], quote: str) -> str:
    """Make one style attribute safe, keeping the original for restoration.

    Two separate concerns. Declarations that let content escape its box are
    dropped outright and never come back. A remote url() is only neutralised
    -- the declaration stays in place so layout survives, and the original
    goes to data-x-style, which the image-policy layer restores from once a
    sender is allowed.
    """
    # nh3 has already run and serialises a `"` inside an attribute value as
    # &quot; -- captured verbatim by _STYLE_RE, that entity text still
    # contains the `;` from inside the quote, so a font stack like
    # font-family:"Open Sans", Arial tokenises into garbage unless it is
    # unescaped back to a literal quote before tinycss2 ever sees it.
    # _attr_value below is what escapes the result again on the way out,
    # exactly once.
    style = html.unescape(match.group(1))
    declarations = _parsed_declarations(style)
    preserved = _serialize_declarations(declarations)
    has_remote = any(_find_remote_url(decl.value) for decl in declarations)

    if not has_remote:
        if preserved == style:
            return match.group(0)
        return f"style={quote}{_attr_value(preserved, quote)}{quote}"

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
    return (
        f"style={quote}{_attr_value(blocked, quote)}{quote} "
        f"data-x-style={quote}{_attr_value(preserved, quote)}{quote}"
    )


# A stylesheet block's own at-rules, allowlisted the same way tags and
# attributes are -- an at-rule outside these two sets is dropped entirely
# rather than inspected, since neither of them is where a mail template
# has a genuine reason to reach.
#
# @media and @supports (and @keyframes, which nests the same shape of
# qualified rule under a keyframe-selector prelude rather than an ordinary
# one) all carry a nested rule list, parsed with parse_rule_list and
# recursed into exactly like the stylesheet's own top level. @font-face
# carries a plain declaration list instead, the same shape a qualified
# rule's own block is.
_RULE_LIST_AT_RULES = frozenset({
    "media", "supports", "keyframes", "-webkit-keyframes", "-moz-keyframes",
})
_DECLARATION_AT_RULES = frozenset({"font-face"})

# A page of CSS is already generous for anything an email template needs;
# beyond it, dropping the block costs nothing an email needs and avoids
# parsing something built to be expensive to parse.
MAX_STYLESHEET_CHARS = 100_000


def _sanitize_declaration_block(
    header: str, content: list[Node],
) -> tuple[str, str, bool] | None:
    """A rule whose block is a plain declaration list -- a qualified rule's
    own body, or an at-rule (@font-face) with no nested rules of its own.

    Exactly the treatment an inline style attribute already gets in
    _rewrite_style, reused rather than reimplemented: escaping declarations
    are dropped outright, a remote url() is only neutralised so layout
    survives, and the original -- url()s intact -- is kept for the same
    sender-gated restoration an inline style's data-x-style already gets.
    Returns None when nothing worth keeping remains.
    """
    declarations = _filter_declarations(
        tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)
    )
    if not declarations:
        return None
    preserved = _serialize_declarations(declarations)
    has_remote = any(_find_remote_url(decl.value) for decl in declarations)
    if not has_remote:
        body = f"{header}{{{preserved}}}"
        return body, body, False

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
    return f"{header}{{{blocked}}}", f"{header}{{{preserved}}}", True


# A selector naming any of these argues with the containment a message is
# rendered inside rather than styling the message's own content -- see
# _selector_escapes_containment. html/body are deliberately not here: a
# real ``<html>``/``<body>`` element never exists for such a selector to
# match in either surface a message's own <style> block survives into --
# neither is in ALLOWED_TAGS above, so nh3 always unwraps a sender's own
# copy, and the isolated shadow root this content is otherwise rendered
# into has no html/body of its own either. Refusing them would cost the
# single most common pattern in real email CSS (a body{} reset) for a
# selector that cannot reach anything.
_FORBIDDEN_SELECTOR_PSEUDOS = frozenset({"host", "host-context", "root"})


def _selector_escapes_containment(prelude: list[Node]) -> bool:
    """Whether a selector's own tokens name the shadow host or the
    isolated stylesheet's own document root.

    `contain: layout paint` on `:host` is what confines a message that
    gets past the sanitizer at all (see email-renderer.tsx) -- a rule
    targeting `:host`, `:host-context()` or `:root` can switch that
    containment off from inside the message's own stylesheet. No
    legitimate mail styles the element it is rendered into.

    Walked at the token level, after tinycss2 has already resolved
    comments and escapes -- the same reason property names are compared
    this way in _filter_declarations: a hex-escaped ``:\\68 ost`` parses
    as the real thing in every browser and would slip a string search
    over the serialized selector. A selector list (``.foo, :host``) is
    one prelude with every branch's tokens present, so scanning the whole
    thing catches a forbidden selector hidden behind an ordinary one
    rather than only the first.
    """
    for i, tok in enumerate(prelude):
        if tok.type == "literal" and tok.value == ":" and i + 1 < len(prelude):
            nxt = prelude[i + 1]
            name = (
                nxt.lower_value if nxt.type == "ident"
                else nxt.lower_name if nxt.type == "function"
                else None
            )
            if name in _FORBIDDEN_SELECTOR_PSEUDOS:
                return True
    return False


def _sanitize_rule(rule: Node) -> tuple[str, str, bool] | None:
    """One top-level or nested rule, reduced to (safe, preserved, has_remote)
    or dropped entirely.

    An at-rule is only ever kept when its name is in one of the two
    allowlists above -- the same allowlist-not-denylist stance ALLOWED_TAGS
    and ALLOWED_ATTRIBUTES already take, rather than a list of at-rules
    considered dangerous so far. @import is refused explicitly regardless
    of what it targets: nothing here needs to pull in a second stylesheet,
    remote or otherwise, and the rest of this module already treats a
    remote fetch as something only an allowlisted sender may cause.
    """
    if rule.type == "qualified-rule":
        if _contains_parse_error(rule.prelude):
            return None
        if _selector_escapes_containment(rule.prelude):
            return None
        selector = tinycss2.serialize(rule.prelude).strip()
        if not selector:
            return None
        return _sanitize_declaration_block(f"{selector} ", rule.content)

    if rule.type != "at-rule" or rule.content is None:
        # Comments and parse errors are already excluded by
        # skip_comments/skip_whitespace above them; an at-rule with no
        # block at all (such as @import) carries nothing a declaration
        # filter can act on.
        return None

    name = rule.lower_at_keyword
    if name == "import":
        return None
    if _contains_parse_error(rule.prelude):
        return None
    prelude = tinycss2.serialize(rule.prelude).strip()
    header = f"@{name} {prelude} " if prelude else f"@{name} "

    if name in _RULE_LIST_AT_RULES:
        inner = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
        safe_inner, preserved_inner, remote = _sanitize_rule_list(inner)
        return f"{header}{{{safe_inner}}}", f"{header}{{{preserved_inner}}}", remote

    if name in _DECLARATION_AT_RULES:
        return _sanitize_declaration_block(header, rule.content)

    return None


def _sanitize_rule_list(rules: list[Node]) -> tuple[str, str, bool]:
    """Every rule at one nesting level, concatenated.

    Shared by the stylesheet's own top level and by @media/@supports/
    @keyframes, which each nest the same shapes one level further in.
    """
    safe_parts: list[str] = []
    preserved_parts: list[str] = []
    has_remote = False
    for rule in rules:
        result = _sanitize_rule(rule)
        if result is None:
            continue
        safe_text, preserved_text, remote = result
        safe_parts.append(safe_text)
        preserved_parts.append(preserved_text)
        has_remote = has_remote or remote
    return "".join(safe_parts), "".join(preserved_parts), has_remote


_STYLE_CLOSE_RE = re.compile(r"</\s*style\b", re.IGNORECASE)


def _reintroduces_a_style_close_tag(text: str) -> bool:
    """Whether a rawtext-terminating sequence appears anywhere in this text.

    A <style> tag's content is never entity-decoded by a browser, so a CSS
    string escape that *decodes* to this sequence -- written by a sender as
    e.g. ``content: "\\3c/style\\3e<script>..."`` -- reintroduces the
    literal characters the moment the parsed value is serialised back out,
    even though nh3's own real HTML parser never saw them contiguous in the
    original message (see _rewrite_style_tag for why that parser is what
    this module trusts for everything else). Nothing legitimate needs this
    literal text in an email's own styling, so the whole stylesheet is
    dropped rather than trusted to still be inert once it is spliced back
    in as tag content -- unlike an inline style attribute's value, tag
    content is exactly where such a sequence is dangerous.
    """
    return bool(_STYLE_CLOSE_RE.search(text))


def _sanitize_stylesheet(css: str) -> tuple[str, str, bool]:
    """A message's own <style> block, sanitised rather than discarded.

    Returns (safe, preserved, has_remote): `safe` is what always renders,
    every remote url() neutralised the same way an inline style's is;
    `preserved` keeps those url()s intact for the sender-gated restoration
    that already governs remote images, once permitted. Media queries,
    including dark-mode ones, survive; @import does not, whatever it
    targets; anything past a generous size is dropped outright rather than
    parsed, so a pathological stylesheet costs nothing to render.
    """
    if len(css) > MAX_STYLESHEET_CHARS:
        return "", "", False
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    safe, preserved, has_remote = _sanitize_rule_list(rules)
    if _reintroduces_a_style_close_tag(safe) or _reintroduces_a_style_close_tag(preserved):
        return "", "", False
    return safe, preserved, has_remote


def refilter_style_declarations(css: str) -> str:
    """Re-run the escaping/parse-error filter over an inline style's
    declarations, url()s left untouched.

    _rewrite_style already applies this exact filter once, at store time,
    to build the value it hands to the sender-gated restoration path in
    image_sanitizer.py -- this is that same filter, exposed so restoring a
    preserved value re-runs it rather than trusts that the stored value
    was actually produced this way. A stored attribute proves nothing
    about how it got there; keeping data-x-style out of the sender-facing
    allowlist above is what closes the direct route, and re-filtering here
    is what stops the two paths drifting apart again in the future.
    """
    return _serialize_declarations(_parsed_declarations(css))


def refilter_stylesheet_css(css: str) -> str:
    """The same re-filtering as refilter_style_declarations, for a whole
    <style> block's preserved content rather than one attribute's value.

    Returns "" if the input is oversized, or would reintroduce a literal
    </style> sequence once spliced back in as the tag's own raw text
    content -- the same two guards _sanitize_stylesheet already applies at
    store time, re-applied here rather than trusted to have already held.
    """
    if len(css) > MAX_STYLESHEET_CHARS:
        return ""
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    _, preserved, _ = _sanitize_rule_list(rules)
    if _reintroduces_a_style_close_tag(preserved):
        return ""
    return preserved


_STYLE_TAG_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)


def _rewrite_style_tag(match: re.Match[str]) -> str:
    """Sanitise one <style> block's content in place, keeping the tag.

    nh3 has already parsed and reserialised the surrounding markup by the
    time this runs, this tag's own rawtext content included -- verified to
    be carried through unescaped, exactly as a real browser's parser would,
    which is what makes matching `</style` on nh3's own output equivalent to
    matching it on the original message. So the text this function reads
    is exactly what a browser would treat as the element's content, and the
    same holds in reverse for what it writes back.
    """
    open_tag, content, close_tag = match.group(1), match.group(2), match.group(3)
    safe, preserved, has_remote = _sanitize_stylesheet(content)
    if not has_remote:
        return f"{open_tag}{safe}{close_tag}"
    quote = '"'
    attr = f' data-x-stylesheet="{_attr_value(preserved, quote)}"'
    return f"{open_tag[:-1]}{attr}>{safe}{close_tag}"


def rewrite_remote_images(html: str) -> str:
    """
    Replace img src, background and CSS url() references with data-x-*.

    CID references (inline MIME images) are preserved as-is. Also used by
    the quote endpoint in api/mails.py, on HTML that has already been
    through sanitize_outbound_html rather than nh3.clean -- outbound's own
    allowlist keeps src attributes quoted the same way nh3 does, which is
    the only thing the regexes above rely on.

    Args:
        html: Sanitized email HTML

    Returns:
        HTML with remote images blocked
    """
    # A <style> block's own content is CSS text, not markup, and the
    # attribute-level rewrites below scan the *whole* string for
    # `style="..."` / `src="..."` -- so it is sanitised and pulled out
    # behind a token first, rather than left in place, for two reasons:
    # CSS text can itself contain those substrings without meaning an
    # attribute (a font stack's `src:` descriptor reads exactly like one),
    # and the safe CSS this step just produced must not be handed to those
    # regexes a second time. NUL bytes cannot appear in HTML nh3 produced,
    # so a token built from one can never collide with real content.
    style_blocks: dict[str, str] = {}

    def _extract_style_block(match: re.Match[str]) -> str:
        token = f"\x00STYLE-BLOCK-{len(style_blocks)}\x00"
        style_blocks[token] = _rewrite_style_tag(match)
        return token

    html = _STYLE_TAG_RE.sub(_extract_style_block, html)
    html = _SRC_RE.sub(_rewrite_src, html)
    html = _SRC_SINGLE_RE.sub(_rewrite_src_single, html)
    html = _BG_RE.sub(_rewrite_bg, html)
    html = _STYLE_RE.sub(lambda m: _rewrite_style(m, '"'), html)
    html = _STYLE_SINGLE_RE.sub(lambda m: _rewrite_style(m, "'"), html)
    for token, value in style_blocks.items():
        html = html.replace(token, value)
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
    # "data" is here so an embedded data: image survives nh3 at all -- it
    # is not a network fetch (see _LOCAL_URL_PREFIXES), and without the
    # scheme allowed nh3 drops the src attribute outright before the
    # rewrite pass below ever gets a chance to recognise it as local and
    # leave it alone.
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags=CONTENT_STRIPPED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https", "mailto", "cid", "data"},
    )
    return rewrite_remote_images(cleaned)
