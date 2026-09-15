"""
mcp_reply.py's pure derivation logic -- no database needed, unlike
tests/pg/test_mcp_reply_mail_pg.py, which proves the same rules end to
end through the reply_mail tool itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from mail_verdict.api.mcp_reply import (
    ForwardDraft,
    ReplyDraft,
    build_quote_wrapper_html,
    derive_forward,
    derive_reply,
    match_identity,
    merge_addresses,
    quoted_plain_text,
)
from mail_verdict.database.models import Identity, Message


def _message(**overrides: object) -> Message:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "folder_id": uuid.uuid4(),
        "thread_id": uuid.uuid4(),
        "from_addr": "Sender Name <sender@example.com>",
        "to_addrs": ["me@example.com"],
        "cc_addrs": None,
        "reply_to": None,
        "subject": "Hello",
        "message_id": "<orig@example.com>",
        "msg_references": None,
        "body_text": "line one\nline two",
        "received_at": datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Message(**defaults)  # type: ignore[arg-type]


class TestDeriveReply:
    def test_replies_to_the_sender_by_default(self) -> None:
        draft = derive_reply(_message(), [], "reply")
        assert draft.to == ["sender@example.com"]
        assert draft.cc == []

    def test_prefers_reply_to_over_from(self) -> None:
        msg = _message(reply_to="Other <other@example.com>")
        draft = derive_reply(msg, [], "reply")
        assert draft.to == ["other@example.com"]

    def test_reply_to_may_list_several_addresses_quoted_commas_ignored(self) -> None:
        msg = _message(reply_to='"Doe, Jane" <jane@example.com>, "Roe, Sam" <sam@example.com>')
        draft = derive_reply(msg, [], "reply")
        assert draft.to == ["jane@example.com", "sam@example.com"]

    def test_subject_gets_a_re_prefix_once(self) -> None:
        assert derive_reply(_message(subject="Hello"), [], "reply").subject == "Re: Hello"
        assert derive_reply(_message(subject="Re: Hello"), [], "reply").subject == "Re: Hello"

    def test_references_chain_appends_the_originals_message_id(self) -> None:
        msg = _message(msg_references=["<a@example.com>"], message_id="<b@example.com>")
        draft = derive_reply(msg, [], "reply")
        assert draft.references == ["<a@example.com>", "<b@example.com>"]

    def test_reply_all_ccs_other_recipients_excluding_own_addresses(self) -> None:
        msg = _message(
            to_addrs=["me@example.com", "other@example.com"], cc_addrs=["third@example.com"],
        )
        draft = derive_reply(msg, ["me@example.com"], "reply_all")
        assert set(draft.cc) == {"other@example.com", "third@example.com"}

    def test_reply_all_never_ccs_the_derived_to_address_again(self) -> None:
        msg = _message(to_addrs=["sender@example.com"])
        draft = derive_reply(msg, [], "reply_all")
        assert "sender@example.com" not in draft.cc

    def test_plain_reply_derives_no_cc_at_all(self) -> None:
        msg = _message(to_addrs=["me@example.com", "other@example.com"])
        draft = derive_reply(msg, [], "reply")
        assert draft.cc == []

    def test_quoted_text_is_appended_to_the_attribution(self) -> None:
        draft = derive_reply(_message(), [], "reply")
        assert draft.quoted_text.startswith(f"\n\n{draft.attribution}\n")
        assert "> line one" in draft.quoted_text
        assert "> line two" in draft.quoted_text


class TestDeriveForward:
    def test_subject_gets_an_fwd_prefix_once(self) -> None:
        assert derive_forward(_message(subject="Hello")).subject == "Fwd: Hello"
        assert derive_forward(_message(subject="Fwd: Hello")).subject == "Fwd: Hello"

    def test_carries_the_original_headers_in_its_attribution(self) -> None:
        msg = _message(from_addr="a@example.com", to_addrs=["b@example.com"], subject="X")
        draft = derive_forward(msg)
        assert "From: a@example.com" in draft.attribution
        assert "To: b@example.com" in draft.attribution
        assert "Subject: X" in draft.attribution


class TestQuotedPlainText:
    def test_prefixes_every_line_with_a_quote_marker(self) -> None:
        out = quoted_plain_text("a\nb", "On x wrote:")
        assert out == "\n\nOn x wrote:\n> a\n> b"

    def test_empty_body_still_produces_the_attribution_line(self) -> None:
        out = quoted_plain_text(None, "On x wrote:")
        assert out == "\n\nOn x wrote:\n> "


class TestBuildQuoteWrapperHtml:
    def test_matches_the_shape_the_web_editors_quote_node_expects(self) -> None:
        out = build_quote_wrapper_html("<p>hi</p>", "On x wrote:")
        assert '<div class="gmail_quote">' in out
        assert '<div class="gmail_attr">On x wrote:</div>' in out
        assert '<blockquote type="cite" class="gmail_quote"' in out
        assert "<p>hi</p>" in out

    def test_a_multiline_attribution_is_joined_with_br_not_newlines(self) -> None:
        out = build_quote_wrapper_html("<p>hi</p>", "line one\nline two")
        assert "line one<br>line two" in out

    def test_escapes_the_attribution_text(self) -> None:
        out = build_quote_wrapper_html("<p>hi</p>", "<script>bad()</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestMatchIdentity:
    def test_matches_case_insensitively(self) -> None:
        identity = Identity(id=uuid.uuid4(), account_id=uuid.uuid4(), email="a@example.com")
        assert match_identity(["A@Example.com"], [identity]) == identity.id

    def test_prefers_the_first_matching_address_in_order(self) -> None:
        to_identity = Identity(id=uuid.uuid4(), account_id=uuid.uuid4(), email="to@example.com")
        cc_identity = Identity(id=uuid.uuid4(), account_id=uuid.uuid4(), email="cc@example.com")
        result = match_identity(["cc@example.com", "to@example.com"], [to_identity, cc_identity])
        assert result == cc_identity.id

    def test_no_match_returns_none(self) -> None:
        identity = Identity(id=uuid.uuid4(), account_id=uuid.uuid4(), email="a@example.com")
        assert match_identity(["b@example.com"], [identity]) is None


class TestMergeAddresses:
    def test_appends_new_addresses_and_dedupes_case_insensitively(self) -> None:
        assert merge_addresses(["a@example.com"], ["A@Example.com", "b@example.com"]) == [
            "a@example.com", "b@example.com",
        ]

    def test_none_leaves_the_base_list_untouched(self) -> None:
        assert merge_addresses(["a@example.com"], None) == ["a@example.com"]


def test_reply_draft_and_forward_draft_are_plain_dataclasses() -> None:
    """Both are duck-typed identically by reply_mail's own `draft` variable
    (`.attribution`, `.quoted_text`, `.subject`) -- guard the shape stays
    that way."""
    reply = ReplyDraft(
        to=[], cc=[], subject="s", quoted_text="q", attribution="a",
        in_reply_to=None, references=None,
    )
    forward = ForwardDraft(subject="s", quoted_text="q", attribution="a")
    for draft in (reply, forward):
        assert draft.subject == "s"
        assert draft.quoted_text == "q"
        assert draft.attribution == "a"
