"""
The orders system prompts. Their wording is measured against known answers
(tests/eval/orders), so each rule a measurement added is pinned here: a
rewording that drops one fails this file and sends its author to the eval.
"""

from __future__ import annotations

import pytest

from mail_verdict.orders.prompts import (
    WRITE_ICONS,
    build_write_system_prompt,
    load_decide_system_prompt,
)


def _line(prompt: str, start: str) -> str:
    return next(line for line in prompt.splitlines() if line.lstrip("- ").startswith(start))


def test_the_write_prompt_never_lets_a_credential_into_the_entry() -> None:
    prompt = build_write_system_prompt(language="English")
    assert "Never write a password, PIN, passcode or other access credential" in prompt


def test_the_icon_rule_says_what_each_icon_is_for() -> None:
    rule = _line(build_write_system_prompt(language="English"), "icon:")
    assert [icon for icon in WRITE_ICONS if icon not in rule] == []


def test_a_tracking_number_is_a_parcel_number_the_mails_state() -> None:
    prompt = build_write_system_prompt(language="English")
    rule = _line(prompt.replace("  - ", ""), "tracking_number:")
    assert "only when a mail states one" in rule and "never the carrier's name" in rule


@pytest.mark.parametrize("kind", ["an e-book", "an audiobook", "an app", "a game"])
def test_digital_goods_bought_for_good_are_orders(kind: str) -> None:
    assert kind in _line(build_write_system_prompt(language="English"), "kind:")


def test_the_write_prompt_is_written_in_the_requested_language_slot() -> None:
    assert "Write in German." in build_write_system_prompt(language="German")


def test_the_decide_prompt_asks_for_identifiers_on_a_new_entry() -> None:
    rule = _line(load_decide_system_prompt(), "identifiers:")
    assert 'whenever the answer is a handle or "new"' in rule
