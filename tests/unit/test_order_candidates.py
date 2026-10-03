"""
orders/candidates.py's rank_orders: the rank table over plain constructed
Order objects, no session or database involved. Plus normalize_identifier
and is_valid_identifier, the number-match building blocks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from mail_verdict.database.models import Order
from mail_verdict.orders.candidates import (
    OrderMails,
    extract_labeled_identifiers,
    is_valid_identifier,
    normalize_identifier,
    rank_orders,
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _order(
    *, order_id: uuid.UUID | None = None, merchant: str = "Shop", subject: str = "Order",
    status: str = "ordered", is_open: bool = True, mail_count: int = 1,
    last_mail_at: datetime | None = None, summary: str = "",
) -> Order:
    return Order(
        id=order_id or uuid.uuid4(), merchant=merchant, subject=subject, status=status,
        is_open=is_open, summary=summary, mail_count=mail_count,
        first_mail_at=last_mail_at, last_mail_at=last_mail_at,
    )


def _rank(
    orders: list[Order],
    *,
    thread_order_ids: set[uuid.UUID] | None = None,
    identifiers_by_order: dict[uuid.UUID, list[tuple[str, str, str]]] | None = None,
    mails_by_order: dict[uuid.UUID, OrderMails] | None = None,
    subject: str = "hello",
    from_addr: str = "someone@example.com",
    haystack_raw: str = "",
) -> list:
    return rank_orders(
        {o.id: o for o in orders},
        thread_order_ids=thread_order_ids or set(),
        identifiers_by_order=identifiers_by_order or {},
        mails_by_order=mails_by_order or {},
        subject=subject, from_addr=from_addr, haystack_raw=haystack_raw, now=_NOW,
    )


def test_a_two_day_old_order_outranks_a_same_sender_order() -> None:
    """Rank 3 (any order active in the last two days) outranks rank 4
    (same sender domain) -- see the module's own docstring."""
    recent = _order(last_mail_at=_NOW - timedelta(hours=1))
    old_same_sender = _order(last_mail_at=_NOW - timedelta(days=10))
    candidates = _rank(
        [recent, old_same_sender], from_addr="me@shop.example",
        mails_by_order={
            old_same_sender.id: OrderMails(froms=("orders@shop.example",), latest=()),
        },
    )
    assert candidates[0].order_id == recent.id


def test_the_cap_of_eight_keeps_the_strongest() -> None:
    orders = [_order(last_mail_at=_NOW - timedelta(hours=h)) for h in range(1, 12)]
    candidates = _rank(orders)
    assert len(candidates) == 8
    # The 8 kept are the 8 most recently active -- rank 3 sorts by
    # last_mail_at descending within the rank.
    kept_ids = {c.order_id for c in candidates}
    expected_ids = {o.id for o in sorted(orders, key=lambda o: o.last_mail_at, reverse=True)[:8]}
    assert kept_ids == expected_ids


def test_a_number_match_respects_word_boundaries() -> None:
    """25927 must not match inside 1259273."""
    order = _order(last_mail_at=_NOW - timedelta(days=1))
    identifiers = {order.id: [("order_number", "25927", "25927")]}
    candidates = _rank(
        [order], identifiers_by_order=identifiers, haystack_raw="tracking 1259273 today",
    )
    assert not any("mail contains" in r for c in candidates for r in c.reasons)


def test_a_number_match_does_fire_on_a_real_boundary() -> None:
    order = _order(last_mail_at=_NOW - timedelta(days=1))
    identifiers = {order.id: [("order_number", "25927", "25927")]}
    candidates = _rank(
        [order], identifiers_by_order=identifiers, haystack_raw="order 25927 shipped",
    )
    assert any("mail contains order_number 25927" in r for r in candidates[0].reasons)


def test_delivery_placeholder_is_not_a_valid_identifier() -> None:
    assert is_valid_identifier("{DELIVERY_0_PARCEL_IDENTCODE}") is False


def test_a_real_tracking_number_is_valid() -> None:
    assert is_valid_identifier("00340434161094043047") is True


def test_normalize_identifier_strips_separators_and_upper_cases() -> None:
    assert normalize_identifier("ab-12.34/56") == "AB123456"


def test_a_thread_match_ranks_above_every_other_rule() -> None:
    thread_order = _order(last_mail_at=_NOW - timedelta(days=200))
    recent_order = _order(last_mail_at=_NOW - timedelta(minutes=1))
    candidates = _rank(
        [thread_order, recent_order], thread_order_ids={thread_order.id},
    )
    assert candidates[0].order_id == thread_order.id
    assert candidates[0].reasons == ("same conversation",)


def test_merchant_name_match_requires_at_least_four_characters() -> None:
    order = _order(merchant="Go", last_mail_at=_NOW - timedelta(days=5))
    candidates = _rank([order], subject="I use Go for scripting", haystack_raw="")
    assert not any("names the merchant" in r for c in candidates for r in c.reasons)


def test_merchant_name_match_fires_for_a_long_enough_name() -> None:
    order = _order(merchant="Audiophonics", last_mail_at=_NOW - timedelta(days=5))
    candidates = _rank([order], subject="Your Audiophonics order has shipped")
    assert "mail names the merchant" in candidates[0].reasons


def test_an_order_with_no_matching_rule_is_not_a_candidate() -> None:
    order = _order(last_mail_at=_NOW - timedelta(days=400))
    assert _rank([order]) == []


def test_the_backstop_takes_a_shipment_number_from_a_link_target() -> None:
    raw = (
        "Your shipment [{DELIVERY_PARCEL_IDENTCODE}]"
        "(https://track.example.com/app/track?piececode=00340000000000000001) is ready."
    )
    assert extract_labeled_identifiers(raw) == [("tracking_number", "00340000000000000001")]


def test_the_backstop_reads_a_link_target_inside_html_with_encoded_ampersands() -> None:
    raw = '<a href="https://track.example.com/t?lang=en&amp;idc=00340000000000000002">x</a>'
    assert extract_labeled_identifiers(raw) == [("tracking_number", "00340000000000000002")]


def test_a_candidate_holding_a_linked_number_ranks_first() -> None:
    held = _order(last_mail_at=_NOW - timedelta(days=5), merchant="Some Shop")
    other = _order(last_mail_at=_NOW - timedelta(days=1), merchant="Another Shop")
    candidates = _rank(
        [held, other],
        identifiers_by_order={
            held.id: [("tracking_number", "00340000000000000001", "00340000000000000001")],
        },
        haystack_raw="https://track.example.com/app/track?piececode=00340000000000000001",
    )
    assert candidates[0].order_id == held.id
    assert any("00340000000000000001" in reason for reason in candidates[0].reasons)
