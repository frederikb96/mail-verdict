"""
Scoring for the orders evaluation: one recorded answer against one case's
expectations, by the cheapest exact means -- fields by equality, prose by a
regex that names its reason, and a few probes that fail when the model
invents something the input never held.

A failure is `hard` when the answer is wrong (a verdict, a kind, a date, an
identifier or code reference the input does not contain, a missing fact) and
`soft` when it only breaks a format the prompt asks for (a label over 30
characters, a duplicate fact). A run passes when it has no hard failure.

The scorer reads nothing but its arguments, so recorded answers can be scored
offline and CI never calls a model.

Expectation keys (all optional):

decide
- `target`: "none", "new" or a handle ("C2")
- `identifiers_include`: values the answer's identifiers must hold

write
- `kind`, `open`: equality
- `event_start`, `event_end`: equality, "" meaning none
- `icon`: the icons that fit what was bought or booked
- `merchant`: regex the merchant must match
- `codes`: exact number of code facts; `codes_min`: lower bound
- `facts_include`: list of `{kind, match, why}`, a fact of that kind whose
  value matches the regex must exist; `kind` may be a list, or "*" for any;
  an optional `carrier` must equal the fact's carrier
- `facts_forbid`: same shape, no such fact may exist
- `text_must_match` / `text_must_not_match`: list of `{pattern, why, field}`
  with `field` one of summary, subject, status, merchant, facts, any
  (default any)
- `max_facts`: upper bound on the fact count
- `secrets_absent`: literal strings (a password or PIN the input holds) that
  must not appear anywhere in the answer
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

IDENTIFIER_KINDS = (
    "order_number", "booking_code", "ticket_number", "invoice_number", "tracking_number",
)
_LABEL_MAX = 30
_VALUE_MAX = 160
_SUBJECT_MAX = 60
_SUMMARY_MAX = 2000
_STATUS_MAX_WORDS = 5


@dataclass
class Result:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard


def normalize(value: str) -> str:
    """Letters and digits only, upper case: how an identifier is compared."""
    folded = unicodedata.normalize("NFKC", value)
    return re.sub(r"[^0-9A-Za-z]", "", folded).upper()


def _kind_matches(wanted: str | list[str], actual: Any) -> bool:
    return wanted == "*" or actual == wanted or (isinstance(wanted, list) and actual in wanted)


def _facts(answer: dict[str, Any]) -> list[dict[str, Any]]:
    facts = answer.get("facts")
    return [f for f in facts if isinstance(f, dict)] if isinstance(facts, list) else []


def _field_text(answer: dict[str, Any], name: str) -> str:
    facts = " ".join(f"{f.get('label', '')} {f.get('value', '')}" for f in _facts(answer))
    if name == "facts":
        return facts
    if name == "any":
        parts = [str(answer.get(k, "")) for k in ("merchant", "subject", "status", "summary")]
        return " ".join([*parts, facts])
    return str(answer.get(name, ""))


def _score_decide(
    answer: dict[str, Any], expect: dict[str, Any], context: dict[str, Any], result: Result,
) -> None:
    if "target" in expect and answer.get("target") != expect["target"]:
        result.hard.append(f"target {answer.get('target')!r}, expected {expect['target']!r}")
    held = {
        normalize(str(i.get("value", ""))) for i in answer.get("identifiers", [])
        if isinstance(i, dict)
    }
    for wanted in expect.get("identifiers_include", []):
        if normalize(wanted) not in held:
            result.hard.append(f"identifier {wanted!r} missing")
    haystack = normalize(context.get("input_text", ""))
    for item in answer.get("identifiers", []):
        if not isinstance(item, dict):
            continue
        value = normalize(str(item.get("value", "")))
        if value and value not in haystack:
            result.hard.append(f"identifier {item.get('value')!r} is not in the input")


def _score_facts(
    answer: dict[str, Any], expect: dict[str, Any], context: dict[str, Any], result: Result,
) -> None:
    facts = _facts(answer)
    haystack = normalize(context.get("input_text", ""))
    markers = set(context.get("markers", []))
    codes = [f for f in facts if f.get("kind") == "code"]

    for fact in facts:
        kind, value = fact.get("kind"), str(fact.get("value", ""))
        if kind in IDENTIFIER_KINDS and normalize(value) not in haystack:
            result.hard.append(f"{kind} {value!r} is not in the input")
        elif kind in IDENTIFIER_KINDS and not any(ch.isdigit() for ch in value):
            result.hard.append(f"{kind} {value!r} is not an identifier")
        if kind == "code":
            ref = fact.get("code_ref")
            if ref not in markers:
                result.hard.append(f"code_ref {ref!r} names no marker")
        if len(str(fact.get("label", ""))) > _LABEL_MAX:
            result.soft.append(f"label over {_LABEL_MAX}: {fact.get('label')!r}")
        if len(value) > _VALUE_MAX:
            result.soft.append(f"value over {_VALUE_MAX} ({kind})")

    seen: set[tuple[Any, str]] = set()
    for fact in facts:
        key = (fact.get("kind"), normalize(str(fact.get("value", ""))) or str(fact.get("code_ref")))
        if key in seen:
            result.soft.append(f"duplicate {key[0]} fact")
        seen.add(key)

    limit = expect.get("max_facts", 12)
    if len(facts) > limit:
        result.hard.append(f"{len(facts)} facts, at most {limit}")
    if "codes" in expect and len(codes) != expect["codes"]:
        result.hard.append(f"{len(codes)} code facts, expected {expect['codes']}")
    if len(codes) < expect.get("codes_min", 0):
        result.hard.append(f"{len(codes)} code facts, at least {expect['codes_min']} expected")

    for want in expect.get("facts_include", []):
        pattern = re.compile(want["match"], re.IGNORECASE)
        if not any(
            _kind_matches(want["kind"], f.get("kind")) and pattern.search(str(f.get("value", "")))
            and want.get("carrier", f.get("carrier")) == f.get("carrier")
            for f in facts
        ):
            result.hard.append(f"no {want['kind']} fact matching /{want['match']}/: {want['why']}")
    for bad in expect.get("facts_forbid", []):
        pattern = re.compile(bad["match"], re.IGNORECASE)
        if any(
            _kind_matches(bad["kind"], f.get("kind")) and pattern.search(str(f.get("value", "")))
            for f in facts
        ):
            result.hard.append(f"{bad['kind']} fact matching /{bad['match']}/: {bad['why']}")


def _score_write(
    answer: dict[str, Any], expect: dict[str, Any], context: dict[str, Any], result: Result,
) -> None:
    for key in ("kind", "open", "event_start", "event_end"):
        if key in expect and answer.get(key) != expect[key]:
            result.hard.append(f"{key} {answer.get(key)!r}, expected {expect[key]!r}")
    if "icon" in expect and answer.get("icon") not in expect["icon"]:
        result.hard.append(f"icon {answer.get('icon')!r}, expected one of {expect['icon']}")
    if "merchant" in expect and not re.search(
        expect["merchant"], str(answer.get("merchant", "")), re.IGNORECASE,
    ):
        result.hard.append(
            f"merchant {answer.get('merchant')!r} does not match /{expect['merchant']}/",
        )

    for rule in expect.get("text_must_match", []):
        text = _field_text(answer, rule.get("field", "any"))
        if not re.search(rule["pattern"], text, re.IGNORECASE | re.DOTALL):
            result.hard.append(
                f"{rule.get('field', 'any')} lacks /{rule['pattern']}/: {rule['why']}",
            )
    for rule in expect.get("text_must_not_match", []):
        text = _field_text(answer, rule.get("field", "any"))
        if re.search(rule["pattern"], text, re.IGNORECASE | re.DOTALL):
            result.hard.append(f"{rule.get('field', 'any')} has /{rule['pattern']}/: {rule['why']}")

    _score_facts(answer, expect, context, result)

    flat = json.dumps(answer, ensure_ascii=False)
    for secret in expect.get("secrets_absent", []):
        if secret in flat:
            result.hard.append("a credential from the input was copied into the answer")

    given = {answer.get("event_start"), answer.get("event_end")} - {"", None}
    for fact in _facts(answer):
        if fact.get("kind") == "date" and str(fact.get("value", "")).strip() in given:
            result.soft.append("date fact repeats event_start or event_end")

    subject, summary = str(answer.get("subject", "")), str(answer.get("summary", ""))
    if len(subject) > _SUBJECT_MAX:
        result.soft.append(f"subject over {_SUBJECT_MAX}")
    if len(summary) > _SUMMARY_MAX:
        result.soft.append(f"summary over {_SUMMARY_MAX}")
    if len(str(answer.get("status", "")).split()) > _STATUS_MAX_WORDS:
        result.soft.append(f"status over {_STATUS_MAX_WORDS} words")
    if re.search(r"^#{1,6}\s|\|\s*---|\]\(http", summary, re.MULTILINE):
        result.soft.append("summary holds a heading, table or link")


def score(
    answer: dict[str, Any] | None, expect: dict[str, Any], context: dict[str, Any],
) -> Result:
    """
    Score one answer.

    Args:
        answer: The parsed model answer, or None when the run ended without one
        expect: The case's expectations (see the module docstring)
        context: `task` ("write", "decide" or "file"), `input_text` (everything
            the model was shown), `markers` (the code markers it was shown,
            "d2:b1" form)
    """
    result = Result()
    if answer is None:
        result.hard.append("no valid answer")
        return result
    if context["task"] == "write":
        _score_write(answer, expect, context, result)
    else:
        _score_decide(answer, expect, context, result)
    return result
