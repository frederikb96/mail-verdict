"""
orders/facts.py parse_written_facts: the write answer's fact list as the
database stores it -- lengths cut, empty values dropped, carriers checked,
twelve at most.
"""

from __future__ import annotations

from mail_verdict.orders.facts import parse_written_facts


def _fact(**changes: str) -> dict[str, str]:
    return {"kind": "place", "label": "Station", "value": "Aachen Hbf", "carrier": "",
            "code_ref": "", **changes}


def test_a_fact_keeps_its_fields_and_empty_markers_become_none() -> None:
    [fact] = parse_written_facts([_fact()])
    assert (fact.kind, fact.label, fact.value, fact.carrier, fact.code_ref) == (
        "place", "Station", "Aachen Hbf", None, None,
    )


def test_label_and_value_are_cut_and_stripped_of_nul() -> None:
    [fact] = parse_written_facts([_fact(label="L" * 80, value="  v\x00" + "x" * 300)])
    assert len(fact.label) == 30 and len(fact.value) == 160
    assert "\x00" not in fact.value and not fact.value.startswith(" ")


def test_a_fact_without_a_value_is_dropped_except_a_code() -> None:
    facts = parse_written_facts([_fact(value=" "), _fact(kind="code", value="", code_ref="d1:b1")])
    assert [f.kind for f in facts] == ["code"]
    assert facts[0].code_ref == "d1:b1"


def test_an_unknown_carrier_is_dropped_and_a_known_one_kept() -> None:
    facts = parse_written_facts([
        _fact(kind="tracking_number", carrier="dhl"),
        _fact(kind="tracking_number", carrier="pigeon"),
    ])
    assert [f.carrier for f in facts] == ["dhl", None]


def test_at_most_twelve_facts_are_kept_in_order() -> None:
    facts = parse_written_facts([_fact(value=f"v{i}") for i in range(20)])
    assert [f.value for f in facts] == [f"v{i}" for i in range(12)]


def test_junk_in_the_list_is_ignored() -> None:
    assert parse_written_facts(["x", 3, None]) == []
    assert parse_written_facts("nothing") == []
