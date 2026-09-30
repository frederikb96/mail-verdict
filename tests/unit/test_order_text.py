"""
orders/text.py: compose_title, summary_preview, and the word-boundary
cuts every write call answer goes through before it is stored.
"""

from __future__ import annotations

from mail_verdict.orders.text import (
    compose_title,
    cut_status,
    cut_subject,
    cut_summary,
    summary_preview,
)


def test_compose_title_joins_subject_and_status_with_an_em_dash() -> None:
    assert compose_title(subject="New shoes", status="shipped") == "New shoes — shipped"


def test_compose_title_is_just_the_subject_when_status_is_empty() -> None:
    assert compose_title(subject="New shoes", status="") == "New shoes"


def test_cut_subject_backs_off_to_the_last_space() -> None:
    subject = "a" * 75 + " " + "b" * 20
    cut = cut_subject(subject)
    assert len(cut) <= 80
    assert cut == "a" * 75


def test_cut_subject_hard_cuts_when_there_is_no_space_to_back_off_to() -> None:
    subject = "a" * 100
    assert cut_subject(subject) == "a" * 80


def test_cut_status_caps_at_sixty_characters() -> None:
    assert len(cut_status("word " * 30)) <= 60


def test_cut_summary_caps_at_fifteen_hundred_characters() -> None:
    assert len(cut_summary("word " * 1000)) <= 1500


def test_summary_preview_drops_bold_markers() -> None:
    assert summary_preview("**Order shipped**") == "Order shipped"


def test_summary_preview_drops_a_leading_bullet_marker() -> None:
    assert summary_preview("- item one\n- item two") == "item one item two"


def test_summary_preview_joins_lines_with_a_space() -> None:
    preview = summary_preview("First sentence.\n\n- a fact\n- another fact")
    assert preview == "First sentence. a fact another fact"


def test_summary_preview_cuts_at_a_word_boundary_at_240_chars() -> None:
    long_summary = "word " * 100
    preview = summary_preview(long_summary)
    assert len(preview) <= 240
    assert not preview.endswith("wor")
