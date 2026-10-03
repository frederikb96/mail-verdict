"""
Deriving what the list and detail screens show from what the write call
stored: an order's title, and the plain-text preview of its markdown
summary. Both computed in one place so the web, iPhone and MCP surfaces
never each grow their own copy.
"""

from __future__ import annotations

import re

_PREVIEW_MAX_CHARS = 240
_SUBJECT_MAX_CHARS = 80
_STATUS_MAX_CHARS = 60
_SUMMARY_MAX_CHARS = 1_500

_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_BULLET_PREFIX_RE = re.compile(r"^[-*]\s+")


def compose_title(*, subject: str, status: str) -> str:
    """`subject + " — " + status`, or just subject when there is no
    status -- an order that has not been written yet, most notably."""
    subject = subject.strip()
    status = status.strip()
    if not status:
        return subject
    return f"{subject} — {status}"


def _cut_at_word_boundary(text: str, max_chars: int) -> str:
    """Cut to at most max_chars, backing off to the last space rather
    than splitting mid-word -- unless the text has no space to back off
    to, in which case a hard cut is the only option left."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut.rstrip()


def cut_subject(subject: str) -> str:
    return _cut_at_word_boundary(subject.strip(), _SUBJECT_MAX_CHARS)


def cut_status(status: str) -> str:
    return _cut_at_word_boundary(status.strip(), _STATUS_MAX_CHARS)


def cut_summary(summary: str) -> str:
    return _cut_at_word_boundary(summary.strip(), _SUMMARY_MAX_CHARS)


def summary_preview(summary: str) -> str:
    """
    Plain text derived from a markdown summary: bold markers dropped, a
    leading bullet marker dropped per line, every line joined with a
    space, whitespace collapsed, cut to 240 characters at a word
    boundary. Not a full markdown parse (see the summary's own subset in
    ui/src/lib/order-summary.ts) -- this is only ever the list row's two
    lines of preview text.
    """
    without_bold = _BOLD_RE.sub(r"\1", summary)
    lines = []
    for line in without_bold.splitlines():
        line = _BULLET_PREFIX_RE.sub("", line.strip())
        if line:
            lines.append(line)
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return _cut_at_word_boundary(joined, _PREVIEW_MAX_CHARS)
