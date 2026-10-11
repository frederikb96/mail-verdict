"""line_diff: changed lines with a little context, unchanged runs collapsed."""

from __future__ import annotations

from mail_verdict.core.line_diff import CONTEXT_LINES, line_diff


def _ops(rows: list) -> list[str]:  # type: ignore[type-arg]
    return [row["op"] for row in rows]


def _text(row: dict) -> str:  # type: ignore[type-arg]
    return "".join(s["text"] for s in row["segments"])


def test_equal_texts_give_no_rows() -> None:
    assert line_diff("a\nb", "a\nb") == []


def test_long_unchanged_runs_collapse_to_gaps() -> None:
    before = "\n".join(f"line {i}" for i in range(20))
    after = before.replace("line 10", "line ten")
    rows = line_diff(before, after)
    assert _ops(rows) == (
        ["gap"] + ["context"] * CONTEXT_LINES + ["removed", "added"]
        + ["context"] * CONTEXT_LINES + ["gap"]
    )
    assert rows[0]["skipped"] == 10 - CONTEXT_LINES
    assert rows[-1]["skipped"] == 20 - 11 - CONTEXT_LINES


def test_a_paired_line_marks_only_the_characters_that_differ() -> None:
    rows = line_diff('"pattern": "@a\\\\.com",', '"pattern": "@a\\\\.com|@b\\\\.de",')
    added = next(r for r in rows if r["op"] == "added")
    assert [s for s in added["segments"] if s["changed"]] == [
        {"text": "|@b\\\\.de", "changed": True},
    ]
    removed = next(r for r in rows if r["op"] == "removed")
    assert not any(s["changed"] for s in removed["segments"])


def test_unrelated_lines_are_not_marked_character_by_character() -> None:
    rows = line_diff("alpha beta", "zzzz qqqq")
    assert all(len(r["segments"]) == 1 and not r["segments"][0]["changed"] for r in rows)


def test_a_missing_side_is_all_added_or_all_removed() -> None:
    assert _ops(line_diff(None, "a\nb")) == ["added", "added"]
    removed = line_diff("a\nb", None)
    assert _ops(removed) == ["removed", "removed"]
    assert [_text(r) for r in removed] == ["a", "b"]
