"""
_to_mail_context (pipeline/stages/match.py): a message's From header,
display name included, must be reduced to a bare address before
sender_match/sender_domain compare against it -- MessageView.from_addr is
the raw header as PostIMAP mirrors it, e.g.
'"Anthropic, PBC" <invoice+statements@mail.anthropic.com>', and neither
condition can ever match a From carrying one otherwise: sender_match's
exact-address comparison and sender_domain's suffix check both fail
against the whole quoted string.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from mail_verdict.pipeline.message_view import FolderView, MessageView
from mail_verdict.pipeline.stages.match import _to_mail_context
from mail_verdict.rules.conditions import evaluate_condition


def _view(from_addr: str) -> MessageView:
    return MessageView(
        message_id=uuid.uuid4(), msg_key="key", account_id=uuid.uuid4(),
        folder=FolderView(id=uuid.uuid4(), imap_name="INBOX", special_use=None),
        subject="Subject", from_addr=from_addr, to_addrs=(), cc_addrs=(),
        headers={}, body="", body_truncated=False, size_bytes=0,
        received_at=None, is_seen=False, is_flagged=False, is_draft=False,
        is_truncated=False, keywords=(), tags=(), attachment_types=(),
        has_attachments=False,
    )


class TestSenderConditionsAgainstADisplayNameFrom:
    def test_sender_match_against_a_full_address_with_a_display_name(self) -> None:
        view = _view('"Anthropic, PBC" <invoice+statements@mail.anthropic.com>')
        ctx = _to_mail_context(view, SimpleNamespace(verdict=None))
        assert evaluate_condition(
            {"sender_match": "invoice+statements@mail.anthropic.com"}, ctx,
        ) is True

    def test_sender_domain_against_a_display_name_from(self) -> None:
        view = _view('"Anthropic, PBC" <invoice+statements@mail.anthropic.com>')
        ctx = _to_mail_context(view, SimpleNamespace(verdict=None))
        assert evaluate_condition({"sender_domain": "mail.anthropic.com"}, ctx) is True

    def test_a_bare_address_from_keeps_working(self) -> None:
        """No display name -- the only shape either condition ever matched
        before this fix -- must not regress."""
        view = _view("alice@example.com")
        ctx = _to_mail_context(view, SimpleNamespace(verdict=None))
        assert evaluate_condition({"sender_match": "alice@example.com"}, ctx) is True
        assert evaluate_condition({"sender_domain": "example.com"}, ctx) is True

    def test_a_display_name_from_a_different_domain_does_not_match(self) -> None:
        view = _view('"Someone" <person@other.example>')
        ctx = _to_mail_context(view, SimpleNamespace(verdict=None))
        assert evaluate_condition({"sender_domain": "mail.anthropic.com"}, ctx) is False
