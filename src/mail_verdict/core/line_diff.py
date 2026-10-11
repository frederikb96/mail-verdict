"""
A line diff of two texts, cut down to what a reader needs: the changed
lines, a few unchanged lines around each change, and a count in place of
every longer unchanged run. Within a changed line paired with its
replacement, the characters that actually differ are marked, so a long
line with one appended alternative does not read as rewritten.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Literal, TypedDict

# Unchanged lines kept on each side of a change.
CONTEXT_LINES = 3
# Below this similarity two paired lines are shown as plainly removed and
# added; marking characters inside them would only scatter highlights.
_INLINE_MIN_RATIO = 0.5

DiffOp = Literal["context", "added", "removed", "gap"]


class DiffSegment(TypedDict):
    text: str
    changed: bool


class DiffRow(TypedDict):
    op: DiffOp
    segments: list[DiffSegment]
    skipped: int


def _row(op: DiffOp, text: str) -> DiffRow:
    return {"op": op, "segments": [{"text": text, "changed": False}], "skipped": 0}


def _gap(count: int) -> DiffRow:
    return {"op": "gap", "segments": [], "skipped": count}


def _inline(old: str, new: str) -> tuple[list[DiffSegment], list[DiffSegment]] | None:
    """Segments of each line with the differing characters marked, or None
    when the two lines are too different for that to help."""
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() < _INLINE_MIN_RATIO:
        return None
    old_segments: list[DiffSegment] = []
    new_segments: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        changed = tag != "equal"
        for segments, text in ((old_segments, old[i1:i2]), (new_segments, new[j1:j2])):
            if not text:
                continue
            if segments and segments[-1]["changed"] == changed:
                segments[-1]["text"] += text
            else:
                segments.append({"text": text, "changed": changed})
    return old_segments, new_segments


def _replace(old_lines: list[str], new_lines: list[str]) -> list[DiffRow]:
    removed = [_row("removed", line) for line in old_lines]
    added = [_row("added", line) for line in new_lines]
    for k in range(min(len(old_lines), len(new_lines))):
        pair = _inline(old_lines[k], new_lines[k])
        if pair is not None:
            removed[k]["segments"], added[k]["segments"] = pair
    return removed + added


def line_diff(before: str | None, after: str | None) -> list[DiffRow]:
    """The rows of a unified diff from `before` to `after`; a missing text
    counts as empty, and two equal texts give no rows."""
    old = (before or "").splitlines()
    new = (after or "").splitlines()
    if old == new:
        return []
    rows: list[DiffRow] = []
    shown_to = 0
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(CONTEXT_LINES):
        if group[0][1] > shown_to:
            rows.append(_gap(group[0][1] - shown_to))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows.extend(_row("context", line) for line in old[i1:i2])
            elif tag == "delete":
                rows.extend(_row("removed", line) for line in old[i1:i2])
            elif tag == "insert":
                rows.extend(_row("added", line) for line in new[j1:j2])
            else:
                rows.extend(_replace(old[i1:i2], new[j1:j2]))
        shown_to = group[-1][2]
    if shown_to < len(old):
        rows.append(_gap(len(old) - shown_to))
    return rows
