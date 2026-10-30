"""
HTML to readable text for mail bodies and HTML attachments.

Block elements break lines, table cells of a row join as `label | value`,
entities are decoded by the parser, and each link with a target becomes a
markdown link (`[text](https://...)`) -- the form orders/content.py already
reads, so a shipment number that exists only in a link's target survives.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

# Private-use markers for the breaks this module inserts, so they stay
# distinguishable from the source's own whitespace, which HTML collapses.
_BREAK = chr(0xE000)
_CELL = chr(0xE001)
_PARAGRAPH = chr(0xE002)
_MARKS = re.escape(_BREAK + _CELL + _PARAGRAPH)
_BREAKS = re.escape(_BREAK + _PARAGRAPH)

_DROP_SELECTOR = "script, style, head, noscript, template, title, svg, object, iframe"
_LINE_BLOCKS = (
    "div, tr, li, section, article, header, footer, nav, aside, main, dt, dd, dl, form, "
    "fieldset, address, figure, figcaption, center"
)
_PARAGRAPH_BLOCKS = "p, ul, ol, table, h1, h2, h3, h4, h5, h6, blockquote, pre, hr"

_SOURCE_SPACE_RE = re.compile(rf"[^\S{_MARKS}]+")
_MARK_SPACE_RE = re.compile(rf" ?([{_MARKS}]) ?")
_CELL_RUN_RE = re.compile(f"{_CELL}+")
_CELL_AT_LINE_END_RE = re.compile(rf"{_CELL}+(?=[{_BREAKS}]|$)")
_LINE_START_CELL_RE = re.compile(rf"(?<=[{_BREAKS}]){_CELL}+|^{_CELL}+")
_BREAK_RUN_RE = re.compile(rf"[{_BREAKS}](?: *[{_BREAKS}])*")
_LINK_SCHEMES = ("http://", "https://")


def html_to_text(html: str) -> str:
    """
    Convert HTML to text with its block and table structure kept.

    Args:
        html: An HTML document or fragment

    Returns:
        Text with one line per block, a blank line around paragraphs and
        tables, `a | b` for the cells of a row, links as markdown; no HTML
        entities, scripts, styles or head content
    """
    tree = LexborHTMLParser(html)
    root = tree.body or tree.root
    if root is None:
        return ""
    for node in tree.css(_DROP_SELECTOR):
        node.decompose()

    for anchor in tree.css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if href.lower().startswith(_LINK_SCHEMES):
            anchor.insert_before("[")
            anchor.insert_after(f"]({href})")
    for node in tree.css("br"):
        node.insert_after(_BREAK)
    for selector, mark in ((_LINE_BLOCKS, _BREAK), (_PARAGRAPH_BLOCKS, _PARAGRAPH)):
        for node in tree.css(selector):
            node.insert_before(mark)
            node.insert_after(mark)
    for node in tree.css("td, th"):
        node.insert_after(_CELL)

    text = root.text(deep=True, separator="", strip=False)
    text = _SOURCE_SPACE_RE.sub(" ", text)
    text = _MARK_SPACE_RE.sub(r"\1", text)
    text = _CELL_RUN_RE.sub(_CELL, text)
    text = _CELL_AT_LINE_END_RE.sub("", text)
    text = _LINE_START_CELL_RE.sub("", text)
    text = text.replace(_CELL, " | ")
    text = _BREAK_RUN_RE.sub(lambda m: "\n\n" if _PARAGRAPH in m.group() else "\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()
