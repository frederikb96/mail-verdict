"""Tests for all 14 condition types + composite conditions (all/any/not)."""

from __future__ import annotations

from typing import Any

from mail_verdict.rules.conditions import (
    ConditionEvaluator,
    MailContext,
    evaluate_condition,
)


def _ctx(**kwargs: Any) -> MailContext:
    """Create a MailContext with overrides."""
    return MailContext(**kwargs)


class TestSubjectContains:
    """Tests for subject_contains condition."""

    def test_match(self) -> None:
        ctx = _ctx(subject="Important Invoice Attached")
        assert evaluate_condition({"subject_contains": "invoice"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(subject="Meeting notes")
        assert evaluate_condition({"subject_contains": "invoice"}, ctx) is False

    def test_case_insensitive(self) -> None:
        ctx = _ctx(subject="INVOICE ENCLOSED")
        assert evaluate_condition({"subject_contains": "invoice"}, ctx) is True


class TestBodyContains:
    """Tests for body_contains condition."""

    def test_match(self) -> None:
        ctx = _ctx(body_text="Please find the invoice attached")
        assert evaluate_condition({"body_contains": "invoice"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(body_text="See you tomorrow")
        assert evaluate_condition({"body_contains": "invoice"}, ctx) is False


class TestSubjectMatches:
    """Tests for subject_matches (regex) condition."""

    def test_regex_match(self) -> None:
        ctx = _ctx(subject="Invoice #12345")
        assert evaluate_condition({"subject_matches": r"Invoice #\d+"}, ctx) is True

    def test_regex_no_match(self) -> None:
        ctx = _ctx(subject="Hello World")
        assert evaluate_condition({"subject_matches": r"Invoice #\d+"}, ctx) is False

    def test_invalid_regex(self) -> None:
        ctx = _ctx(subject="test")
        assert evaluate_condition({"subject_matches": "[invalid"}, ctx) is False


class TestBodyMatches:
    """Tests for body_matches (regex) condition."""

    def test_regex_match(self) -> None:
        ctx = _ctx(body_text="Amount: $150.00")
        assert evaluate_condition({"body_matches": r"\$\d+\.\d{2}"}, ctx) is True


class TestSenderMatch:
    """Tests for sender_match (address or domain)."""

    def test_full_address_match(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        assert evaluate_condition({"sender_match": "alice@example.com"}, ctx) is True

    def test_domain_match(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        assert evaluate_condition({"sender_match": "example.com"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        assert evaluate_condition({"sender_match": "bob@other.com"}, ctx) is False


class TestSenderDomain:
    """Tests for sender_domain condition."""

    def test_match(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        assert evaluate_condition({"sender_domain": "example.com"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(from_addr="alice@example.com")
        assert evaluate_condition({"sender_domain": "other.com"}, ctx) is False


class TestHeaderMatch:
    """Tests for header_match(field, pattern) condition."""

    def test_match(self) -> None:
        ctx = _ctx(raw_headers={"X-Mailer": "Thunderbird 102"})
        cond = {"header_match": {"field": "X-Mailer", "pattern": "Thunderbird"}}
        assert evaluate_condition(cond, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(raw_headers={"X-Mailer": "Outlook"})
        cond = {"header_match": {"field": "X-Mailer", "pattern": "Thunderbird"}}
        assert evaluate_condition(cond, ctx) is False


class TestHeaderExists:
    """Tests for header_exists condition."""

    def test_exists(self) -> None:
        ctx = _ctx(raw_headers={"X-Priority": "1"})
        assert evaluate_condition({"header_exists": "X-Priority"}, ctx) is True

    def test_not_exists(self) -> None:
        ctx = _ctx(raw_headers={})
        assert evaluate_condition({"header_exists": "X-Priority"}, ctx) is False


class TestSizeConditions:
    """Tests for size_gt and size_lt conditions."""

    def test_size_gt_match(self) -> None:
        ctx = _ctx(size_bytes=10000)
        assert evaluate_condition({"size_gt": 5000}, ctx) is True

    def test_size_gt_no_match(self) -> None:
        ctx = _ctx(size_bytes=1000)
        assert evaluate_condition({"size_gt": 5000}, ctx) is False

    def test_size_lt_match(self) -> None:
        ctx = _ctx(size_bytes=1000)
        assert evaluate_condition({"size_lt": 5000}, ctx) is True

    def test_size_lt_no_match(self) -> None:
        ctx = _ctx(size_bytes=10000)
        assert evaluate_condition({"size_lt": 5000}, ctx) is False


class TestHasAttachment:
    """Tests for has_attachment condition."""

    def test_bool_true(self) -> None:
        ctx = _ctx(has_attachments=True)
        assert evaluate_condition({"has_attachment": True}, ctx) is True

    def test_bool_false(self) -> None:
        ctx = _ctx(has_attachments=False)
        assert evaluate_condition({"has_attachment": True}, ctx) is False

    def test_type_filter(self) -> None:
        ctx = _ctx(has_attachments=True, attachment_types=["application/pdf"])
        assert evaluate_condition({"has_attachment": "application/pdf"}, ctx) is True

    def test_type_filter_no_match(self) -> None:
        ctx = _ctx(has_attachments=True, attachment_types=["image/png"])
        assert evaluate_condition({"has_attachment": "application/pdf"}, ctx) is False


class TestFolderIs:
    """Tests for folder_is condition."""

    def test_match(self) -> None:
        ctx = _ctx(folder="INBOX")
        assert evaluate_condition({"folder_is": "inbox"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(folder="Sent")
        assert evaluate_condition({"folder_is": "INBOX"}, ctx) is False


class TestTagIs:
    """Tests for tag_is condition."""

    def test_match(self) -> None:
        ctx = _ctx(tags=["billing", "urgent"])
        assert evaluate_condition({"tag_is": "billing"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(tags=["personal"])
        assert evaluate_condition({"tag_is": "billing"}, ctx) is False


class TestEnrichmentTag:
    """Tests for enrichment_tag condition."""

    def test_match(self) -> None:
        ctx = _ctx(enrichment_tags=["priority", "action-required"])
        assert evaluate_condition({"enrichment_tag": "priority"}, ctx) is True

    def test_no_match(self) -> None:
        ctx = _ctx(enrichment_tags=[])
        assert evaluate_condition({"enrichment_tag": "priority"}, ctx) is False


class TestUnknownCondition:
    """Tests for unknown condition type."""

    def test_returns_false(self) -> None:
        ctx = _ctx()
        evaluator = ConditionEvaluator()
        assert evaluator.evaluate({"unknown_type": "value"}, ctx) is False


class TestCompositeConditions:
    """Tests for all/any/not composition."""

    def test_all_true(self) -> None:
        ctx = _ctx(subject="Invoice", body_text="Payment required")
        cond = {"all": [
            {"subject_contains": "invoice"},
            {"body_contains": "payment"},
        ]}
        assert evaluate_condition(cond, ctx) is True

    def test_all_partial_false(self) -> None:
        ctx = _ctx(subject="Invoice", body_text="Hello")
        cond = {"all": [
            {"subject_contains": "invoice"},
            {"body_contains": "payment"},
        ]}
        assert evaluate_condition(cond, ctx) is False

    def test_any_one_matches(self) -> None:
        ctx = _ctx(subject="Invoice")
        cond = {"any": [
            {"subject_contains": "invoice"},
            {"subject_contains": "receipt"},
        ]}
        assert evaluate_condition(cond, ctx) is True

    def test_any_none_matches(self) -> None:
        ctx = _ctx(subject="Hello")
        cond = {"any": [
            {"subject_contains": "invoice"},
            {"subject_contains": "receipt"},
        ]}
        assert evaluate_condition(cond, ctx) is False

    def test_not_negates(self) -> None:
        ctx = _ctx(subject="Invoice")
        cond = {"not": {"subject_contains": "invoice"}}
        assert evaluate_condition(cond, ctx) is False

    def test_not_negates_false(self) -> None:
        ctx = _ctx(subject="Hello")
        cond = {"not": {"subject_contains": "invoice"}}
        assert evaluate_condition(cond, ctx) is True

    def test_nested_composition(self) -> None:
        """Nested all/any/not works."""
        ctx = _ctx(subject="Invoice", from_addr="alice@example.com")
        cond = {
            "all": [
                {"subject_contains": "invoice"},
                {"not": {"sender_match": "spam.com"}},
            ]
        }
        assert evaluate_condition(cond, ctx) is True

    def test_empty_condition_dict(self) -> None:
        """Empty condition dict returns False."""
        ctx = _ctx()
        evaluator = ConditionEvaluator()
        assert evaluator.evaluate({}, ctx) is False
