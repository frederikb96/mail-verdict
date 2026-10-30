"""
The orders evaluation's scorer (tests/eval/orders/scorer.py), offline: model
answers recorded from earlier runs are scored again and every rule is
exercised on a small hand-made answer. No model is called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.eval.orders import scorer

_EVAL = Path(__file__).parent.parent / "eval" / "orders"
_EXPECTATIONS = yaml.safe_load((_EVAL / "expectations.yaml").read_text())["cases"]
_KNOWN_KEYS = {
    "target", "identifiers_include", "kind", "open", "event_start", "event_end", "icon",
    "merchant", "codes", "codes_min", "facts_include", "facts_forbid", "text_must_match",
    "text_must_not_match", "max_facts", "secrets_absent",
}


def _context(name: str, markers: list[str]) -> dict[str, Any]:
    case = json.loads((_EVAL / "cases" / f"{name}.json").read_text())
    return {
        "task": "write" if case["task"] == "write" else case["task"],
        "input_text": json.dumps(case, ensure_ascii=False), "markers": markers,
    }


def _fact(
    kind: str, value: str, *, label: str = "L", carrier: str = "", ref: str = "",
) -> dict[str, str]:
    return {"kind": kind, "label": label, "value": value, "carrier": carrier, "code_ref": ref}


def _answer(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "merchant": "Bahnwerk", "subject": "Berlin to Leipzig", "status": "ticket issued",
        "open": True, "kind": "ticket", "event_start": "2026-03-06", "event_end": "",
        "expected_until": "2026-03-06", "icon": "train", "summary": "A flex ticket.", "facts": [],
    }
    return {**base, **fields}


_WRITE_CONTEXT = {
    "task": "write", "input_text": "Order BW-77120934 DHL 00340434161094015902",
    "markers": ["d1:b1"],
}


def _hard(
    answer: dict[str, Any], expect: dict[str, Any], context: dict[str, Any] | None = None,
) -> list[str]:
    return scorer.score(answer, expect, context or _WRITE_CONTEXT).hard


def test_every_expectation_names_a_case_and_every_case_has_expectations() -> None:
    cases = {p.stem for p in (_EVAL / "cases").glob("*.json")}
    assert cases == set(_EXPECTATIONS)


def test_expectations_use_only_keys_the_scorer_reads() -> None:
    assert {k for e in _EXPECTATIONS.values() for k in e} <= _KNOWN_KEYS


@pytest.mark.parametrize("path", sorted((_EVAL / "recorded").glob("*.json")), ids=lambda p: p.stem)
def test_a_recorded_answer_scores_as_it_did_when_it_was_recorded(path: Path) -> None:
    record = json.loads(path.read_text())
    context = _context(record["case"], record["markers"])
    result = scorer.score(record["answer"], _EXPECTATIONS[record["case"]], context)
    assert result.ok == (not record["hard"])


def test_a_credential_copied_from_the_input_fails() -> None:
    record = json.loads((_EVAL / "recorded" / "hotel_with_pin.qwen-low.json").read_text())
    result = scorer.score(
        record["answer"], _EXPECTATIONS["hotel_with_pin"], _context("hotel_with_pin", []),
    )
    assert any("credential" in message for message in result.hard)


def test_a_missing_answer_is_a_hard_failure() -> None:
    assert scorer.score(None, {}, _WRITE_CONTEXT).hard == ["no valid answer"]


def test_fields_are_compared_by_equality() -> None:
    expect = {"kind": "order", "open": False, "event_start": "", "event_end": ""}
    assert len(_hard(_answer(), expect)) == 3


def test_an_icon_outside_the_allowed_set_fails() -> None:
    assert _hard(_answer(icon="bed"), {"icon": ["train"]})
    assert not _hard(_answer(icon="train"), {"icon": ["train"]})


def test_an_identifier_the_input_never_held_is_invented() -> None:
    answer = _answer(facts=[_fact("order_number", "ZZ-99999999")])
    assert any("not in the input" in m for m in _hard(answer, {}))


def test_a_carrier_name_is_not_a_tracking_number() -> None:
    context = {**_WRITE_CONTEXT, "input_text": "Shipped with DHL, number follows"}
    answer = _answer(facts=[_fact("tracking_number", "dhl", carrier="dhl")])
    assert any("not an identifier" in m for m in _hard(answer, {}, context))


def test_a_code_must_name_a_marker_the_model_was_shown() -> None:
    assert _hard(_answer(facts=[_fact("code", "", ref="d9:b1")]), {})
    assert not _hard(_answer(facts=[_fact("code", "", ref="d1:b1")]), {})


def test_the_number_of_codes_is_counted() -> None:
    answer = _answer(facts=[_fact("code", "", ref="d1:b1")])
    assert not _hard(answer, {"codes": 1, "codes_min": 1})
    assert _hard(answer, {"codes": 2})


def test_a_required_fact_must_match_in_kind_and_value() -> None:
    answer = _answer(facts=[_fact("order_number", "BW-77120934")])
    want = {"kind": ["order_number", "booking_code"], "match": "BW-?77120934", "why": "x"}
    assert not _hard(answer, {"facts_include": [want]})
    assert _hard(answer, {"facts_include": [{**want, "kind": "amount"}]})
    assert not _hard(answer, {"facts_include": [{**want, "kind": "*"}]})


def test_a_required_carrier_must_equal_the_facts_carrier() -> None:
    want = {"kind": "tracking_number", "match": "0034", "carrier": "dhl", "why": "x"}
    ok = _answer(facts=[_fact("tracking_number", "00340434161094015902", carrier="dhl")])
    wrong = _answer(facts=[_fact("tracking_number", "00340434161094015902", carrier="ups")])
    assert not _hard(ok, {"facts_include": [want]})
    assert _hard(wrong, {"facts_include": [want]})


def test_a_forbidden_fact_fails_when_present() -> None:
    answer = _answer(facts=[_fact("tracking_number", "00340434161094015902", carrier="dhl")])
    assert _hard(answer, {"facts_forbid": [{"kind": "tracking_number", "match": ".", "why": "x"}]})


def test_prose_rules_check_the_named_field() -> None:
    answer = _answer(subject="Leipzig trip", summary="Nothing here.")
    in_subject = {"pattern": "Leipzig", "field": "subject", "why": "x"}
    assert not _hard(answer, {"text_must_match": [in_subject]})
    assert _hard(answer, {"text_must_match": [{**in_subject, "field": "summary"}]})
    assert _hard(answer, {"text_must_not_match": [{"pattern": "Leipzig", "why": "x"}]})


def test_more_facts_than_allowed_fail() -> None:
    facts = [_fact("other", f"v{i}") for i in range(13)]
    assert _hard(_answer(facts=facts), {})


def test_format_slips_are_soft() -> None:
    answer = _answer(
        subject="s" * 61, status="one two three four five six",
        facts=[_fact("date", "2026-03-06"), _fact("other", "same"), _fact("other", "same")],
    )
    result = scorer.score(answer, {}, _WRITE_CONTEXT)
    assert result.ok and len(result.soft) == 4


def test_a_decide_answer_is_checked_for_target_and_invented_identifiers() -> None:
    context = {"task": "decide", "input_text": "Order BW-77120934", "markers": []}
    good = {"target": "new", "identifiers": [{"kind": "order_number", "value": "BW-77120934"}]}
    wanted = {"identifiers_include": ["BW-77120934"]}
    assert not scorer.score(good, {"target": "new", **wanted}, context).hard
    assert scorer.score({**good, "target": "none"}, {"target": "new"}, context).hard
    assert scorer.score({"target": "new", "identifiers": []}, wanted, context).hard
    invented = {"target": "new", "identifiers": [{"kind": "order_number", "value": "QQ-1"}]}
    assert scorer.score(invented, {}, context).hard
