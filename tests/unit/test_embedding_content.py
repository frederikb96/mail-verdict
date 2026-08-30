"""
Unit tests for embeddings/content.py.
"""

from __future__ import annotations

from mail_verdict.embeddings.content import (
    CONTENT_LEVEL_ENVELOPE,
    CONTENT_LEVEL_FULL,
    build_embedding_input,
)


def test_ordinary_message_gets_full_content_level() -> None:
    """A message with a plain-text body is embedded in full, subject and
    sender first."""
    result = build_embedding_input(
        subject="Invoice #42", from_addr="billing@example.com",
        body_text="Please find your invoice attached.", body_html=None,
        is_truncated=False, content_chars=2000,
    )
    assert result.content_level == CONTENT_LEVEL_FULL
    assert result.text.startswith("Subject: Invoice #42\nFrom: billing@example.com\n")
    assert "Please find your invoice attached." in result.text


def test_truncated_message_gets_envelope_only() -> None:
    """is_truncated means PostIMAP never fetched the body -- body_text/html
    are meaningless and must not be used even if somehow non-empty."""
    result = build_embedding_input(
        subject="Big attachment", from_addr="sender@example.com",
        body_text="this should be ignored", body_html=None,
        is_truncated=True, content_chars=2000,
    )
    assert result.content_level == CONTENT_LEVEL_ENVELOPE
    assert "this should be ignored" not in result.text
    assert "Subject: Big attachment" in result.text


def test_html_only_message_is_stripped_to_text() -> None:
    """A message with no body_text but an HTML body still gets full-level
    content -- the HTML is stripped, not skipped."""
    result = build_embedding_input(
        subject="Newsletter", from_addr="news@example.com", body_text=None,
        body_html="<p>Big <b>sale</b> this week!</p>", is_truncated=False,
        content_chars=2000,
    )
    assert result.content_level == CONTENT_LEVEL_FULL
    assert "Big" in result.text
    assert "sale" in result.text
    assert "<p>" not in result.text
    assert "<b>" not in result.text


def test_genuinely_empty_body_gets_envelope_only() -> None:
    """No body at all, not truncated -- still gets a vector, from envelope alone."""
    result = build_embedding_input(
        subject="(no subject)", from_addr="sender@example.com", body_text=None,
        body_html=None, is_truncated=False, content_chars=2000,
    )
    assert result.content_level == CONTENT_LEVEL_ENVELOPE


def test_body_is_truncated_to_content_chars() -> None:
    """The body is capped at content_chars, so a very long message does not
    dilute the vector with more of a reply chain than intended."""
    result = build_embedding_input(
        subject="Long", from_addr="sender@example.com", body_text="x" * 5000,
        body_html=None, is_truncated=False, content_chars=100,
    )
    body_line = result.text.split("\n", 2)[2]
    assert len(body_line) == 100


def test_source_hash_is_stable_for_identical_input() -> None:
    """Re-encoding the same content must produce the same hash, so a
    re-encode pass can skip rows whose input has not changed."""
    kwargs = dict(
        subject="Hi", from_addr="a@example.com", body_text="same body",
        body_html=None, is_truncated=False, content_chars=2000,
    )
    a = build_embedding_input(**kwargs)  # type: ignore[arg-type]
    b = build_embedding_input(**kwargs)  # type: ignore[arg-type]
    assert a.source_hash == b.source_hash


def test_source_hash_changes_when_text_changes() -> None:
    """A different body must produce a different hash."""
    a = build_embedding_input(
        subject="Hi", from_addr="a@example.com", body_text="first version",
        body_html=None, is_truncated=False, content_chars=2000,
    )
    b = build_embedding_input(
        subject="Hi", from_addr="a@example.com", body_text="second version",
        body_html=None, is_truncated=False, content_chars=2000,
    )
    assert a.source_hash != b.source_hash
