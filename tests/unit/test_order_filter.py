"""
orders/filter.py's evaluate_filter: include/exclude semantics, the
first-match reason, case-insensitivity -- plus the two real-mail cases
the design was tuned against (a Saferpay-style subject passing on
"zahlung", a GLS bank sender not passing on the carrier pattern) -- and
settings/orders_validation.py's write-time guard.
"""

from __future__ import annotations

import pytest

from mail_verdict.orders.filter import evaluate_filter
from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory
from mail_verdict.settings.orders_validation import validate_orders_settings

_DEFAULT_FILTER = SETTING_DEFAULTS[SettingCategory.ORDERS]["filter"]


def test_a_subject_pattern_matches_and_reports_its_reason() -> None:
    result = evaluate_filter(
        subject="Ihre Bestellung ist unterwegs", from_header="shop@example.com",
        body="", patterns=_DEFAULT_FILTER,
    )
    assert result.passed is True
    assert result.reason == "subject:bestell"


def test_matching_is_case_insensitive() -> None:
    result = evaluate_filter(
        subject="YOUR ORDER HAS SHIPPED", from_header="a@b.com", body="",
        patterns=_DEFAULT_FILTER,
    )
    assert result.passed is True


def test_no_include_pattern_matching_fails_the_filter() -> None:
    result = evaluate_filter(
        subject="Weekly newsletter", from_header="news@example.com", body="Nothing here.",
        patterns=_DEFAULT_FILTER,
    )
    assert result.passed is False
    assert result.reason is None


def test_an_exclude_pattern_wins_even_when_an_include_pattern_also_matches() -> None:
    patterns = {
        "include": {"subject": ["order"]},
        "exclude": {"subject": ["order"]},
    }
    result = evaluate_filter(
        subject="Your order", from_header="a@b.com", body="", patterns=patterns,
    )
    assert result.passed is False


def test_the_reason_is_the_first_match_in_subject_from_body_order() -> None:
    patterns = {
        "include": {
            "subject": ["nomatch"], "from": ["shop"], "body": ["invoice"],
        },
    }
    result = evaluate_filter(
        subject="hello", from_header="shop@example.com", body="an invoice is attached",
        patterns=patterns,
    )
    assert result.reason == "from:shop"


def test_saferpay_style_payment_subject_passes_on_zahlung() -> None:
    """The mail survey's one reported miss (design section 3.4) -- now
    covered by the "zahlung" pattern."""
    result = evaluate_filter(
        subject="Vielen Dank für Ihre Zahlung", from_header="noreply@saferpay.com", body="",
        patterns=_DEFAULT_FILTER,
    )
    assert result.passed is True


def test_gls_the_bank_does_not_pass_on_the_carrier_pattern() -> None:
    """gls.de is GLS Gemeinschaftsbank, not the GLS parcel carrier -- the
    default `from` patterns name the carrier's own domains
    (gls-group/gls-pakete/gls-germany), never bare "gls"."""
    result = evaluate_filter(
        subject="Ihr Kontoauszug", from_header="service@gls.de", body="", patterns=_DEFAULT_FILTER,
    )
    assert result.passed is False


def test_validate_orders_settings_accepts_the_shipped_defaults() -> None:
    validate_orders_settings({"filter": _DEFAULT_FILTER})


def test_validate_orders_settings_rejects_an_uncompilable_pattern() -> None:
    with pytest.raises(ValueError, match="does not compile"):
        validate_orders_settings({"filter": {"include": {"subject": ["("]}}})


def test_validate_orders_settings_rejects_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        validate_orders_settings({"filter": {"include": {"nope": ["x"]}}})


def test_validate_orders_settings_rejects_a_non_string_pattern() -> None:
    with pytest.raises(ValueError, match="list of strings"):
        validate_orders_settings({"filter": {"include": {"subject": [123]}}})


def test_validate_orders_settings_rejects_too_many_patterns() -> None:
    patterns = {"include": {"subject": [f"p{i}" for i in range(400)]}}
    with pytest.raises(ValueError, match="more than the 300 allowed"):
        validate_orders_settings({"filter": patterns})


def test_validate_orders_settings_rejects_an_out_of_range_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_orders_settings({"reasoning_effort": "xhigh"})


def test_validate_orders_settings_rejects_max_tokens_out_of_range() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        validate_orders_settings({"max_tokens": 100})
