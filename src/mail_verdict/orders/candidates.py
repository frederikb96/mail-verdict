"""
Deterministic candidate retrieval for the orders decide call: which
existing orders a new mail might belong to, ranked and capped at 8.

An order qualifies through any of the rules below; its rank is the best
(lowest-numbered) rule that matched, and within a rank the order with the
newest last_mail_at sorts first. Every reason that matched an order is
kept and shown to the model, not just the best one -- see prompts.py for
how a Candidate is rendered into the decide prompt.

Rank 3 (any order active in the last two days) deliberately outranks rank
4 (same sender domain): with sender ranked above recency, an order that
ever received a PayPal receipt outranks one opened a minute earlier, and
the just-opened order falls off the list of 8 (observed during design).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Order, OrderIdentifier, OrderMail

_MAX_CANDIDATES = 8

# Rules 1-6's windows, in days; rule 0 (thread) has none. Rule 3's window
# is a special case handled inline (hours, not days).
_NUMBER_WINDOW_DAYS = 365
_MERCHANT_WINDOW_DAYS = 90
_RECENT_WINDOW_HOURS = 48
_SENDER_DOMAIN_WINDOW_DAYS = 90
_STATUS_WINDOW_DAYS = 14
# The widest window any ranked rule (other than thread) ever uses -- the
# ceiling this module loads candidate orders under before ranking them.
_LOAD_WINDOW_DAYS = _NUMBER_WINDOW_DAYS

_MIN_MERCHANT_LENGTH = 4

_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./_#:-]{3,39}$")
_NORM_STRIP_RE = re.compile(r"[ \-_./#]")


def normalize_identifier(value: str) -> str:
    """Upper-case, with space/-/_/.///# removed -- what an identifier's
    value_norm column stores, and what a long identifier's fuzzy match
    compares against."""
    return _NORM_STRIP_RE.sub("", value).upper()


def is_valid_identifier(value: str) -> bool:
    """
    Whether a model-reported identifier is worth storing at all.

    Guards against an unfilled template placeholder such as DHL's own
    `{DELIVERY_0_PARCEL_IDENTCODE}` being stored as a real tracking number
    and then "matching" the next mail from the same carrier (observed
    during design).
    """
    trimmed = value.strip()
    if not _VALID_IDENTIFIER_RE.match(trimmed):
        return False
    norm = normalize_identifier(trimmed)
    digit_count = sum(1 for ch in norm if ch.isdigit())
    return len(norm) >= 5 and digit_count >= 3


def _number_matches(value: str, value_norm: str, haystack_upper: str, haystack_norm: str) -> bool:
    """One identifier against one mail's haystack -- word-boundary match
    on the literal value, or, for a long identifier, a substring match on
    both sides normalised (catches the same number written with
    different spacing/punctuation)."""
    escaped = re.escape(value.upper())
    if re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", haystack_upper):
        return True
    return len(value_norm) >= 8 and value_norm in haystack_norm


def _sender_domain(addr: str) -> str | None:
    """The last two labels of an address's domain -- "amazon.de" from
    both "orders@amazon.de" and "noreply@marketing.amazon.de"."""
    if "@" not in addr:
        return None
    domain = addr.rsplit("@", 1)[1].lower().strip()
    labels = domain.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else domain or None


@dataclass(frozen=True)
class CandidateMail:
    received_at: datetime
    from_addr: str
    subject: str


@dataclass(frozen=True)
class Candidate:
    """One order offered to the decide call, with its handle (C1, C2, ...)
    assigned by the caller once the final ranked list is known."""

    order_id: uuid.UUID
    merchant: str
    subject: str
    status: str
    is_open: bool
    summary: str
    mail_count: int
    first_mail_at: datetime | None
    last_mail_at: datetime | None
    numbers: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]
    latest_mails: tuple[CandidateMail, ...]
    rank: int = field(compare=False, default=99)


async def find_candidates(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    thread_id: uuid.UUID | None,
    subject: str,
    from_addr: str,
    haystack_raw: str,
    now: datetime | None = None,
) -> list[Candidate]:
    """
    Rank and return at most 8 candidate orders for one new mail.

    Args:
        session: Session to read through
        account_id: The mail's account
        thread_id: The mail's thread, if any
        subject: The mail's Subject header
        from_addr: The mail's From address
        haystack_raw: Untouched text (subject + raw body) a number search
            scans -- see orders/content.py's PreparedBody.raw
        now: Injected for tests; defaults to the current UTC time

    Returns:
        Candidates ranked best-first, capped at 8
    """
    now = now or datetime.now(timezone.utc)
    haystack_upper = f"{subject}\n{haystack_raw}".upper()
    haystack_norm = normalize_identifier(haystack_upper)
    sender_domain = _sender_domain(from_addr)

    thread_order_ids: set[uuid.UUID] = set()
    if thread_id is not None:
        result = await session.execute(
            select(OrderMail.order_id.distinct()).where(
                OrderMail.account_id == account_id, OrderMail.thread_id == thread_id,
            )
        )
        thread_order_ids = {row[0] for row in result.all()}

    cutoff = now - timedelta(days=_LOAD_WINDOW_DAYS)
    windowed_result = await session.execute(
        select(Order).where(Order.last_mail_at.is_not(None), Order.last_mail_at >= cutoff)
    )
    windowed_orders = list(windowed_result.scalars().all())

    order_by_id: dict[uuid.UUID, Order] = {o.id: o for o in windowed_orders}
    if thread_order_ids - order_by_id.keys():
        extra_result = await session.execute(
            select(Order).where(Order.id.in_(thread_order_ids - order_by_id.keys()))
        )
        for order in extra_result.scalars().all():
            order_by_id[order.id] = order

    if not order_by_id:
        return []

    order_ids = list(order_by_id)
    identifiers_by_order = await _load_identifiers(session, order_ids)
    mails_by_order = await _load_mails(session, order_ids)

    ranked: list[Candidate] = []
    for order_id, order in order_by_id.items():
        reasons: list[str] = []
        best_rank = 99

        if order_id in thread_order_ids:
            reasons.append("same conversation")
            best_rank = min(best_rank, 0)

        order_numbers = identifiers_by_order.get(order_id, ())
        matched_numbers = [
            (kind, value) for kind, value, value_norm in order_numbers
            if _number_matches(value, value_norm, haystack_upper, haystack_norm)
        ]
        if matched_numbers and _within_days(order.last_mail_at, now, _NUMBER_WINDOW_DAYS):
            for kind, value in matched_numbers:
                reasons.append(f"mail contains {kind} {value}")
            best_rank = min(best_rank, 1)

        merchant = (order.merchant or "").strip()
        if (
            len(merchant) >= _MIN_MERCHANT_LENGTH
            and merchant.lower() in f"{subject}\n{haystack_raw}".lower()
            and _within_days(order.last_mail_at, now, _MERCHANT_WINDOW_DAYS)
        ):
            reasons.append("mail names the merchant")
            best_rank = min(best_rank, 2)

        if _within_hours(order.last_mail_at, now, _RECENT_WINDOW_HOURS):
            reasons.append("active in the last two days")
            best_rank = min(best_rank, 3)

        order_froms = mails_by_order[order_id].froms if order_id in mails_by_order else ()
        order_domains = {d for d in (_sender_domain(a) for a in order_froms) if d}
        if (
            sender_domain is not None and sender_domain in order_domains
            and _within_days(order.last_mail_at, now, _SENDER_DOMAIN_WINDOW_DAYS)
        ):
            reasons.append("same sender domain")
            best_rank = min(best_rank, 4)

        if _within_days(order.last_mail_at, now, _STATUS_WINDOW_DAYS):
            if order.is_open:
                reasons.append("active in the last two weeks")
                best_rank = min(best_rank, 5)
            else:
                reasons.append("active in the last two weeks")
                best_rank = min(best_rank, 6)

        if not reasons:
            continue

        latest = mails_by_order[order_id].latest if order_id in mails_by_order else ()
        numbers = tuple((kind, value) for kind, value, _ in order_numbers)[:8]
        ranked.append(
            Candidate(
                order_id=order_id, merchant=order.merchant, subject=order.subject,
                status=order.status, is_open=order.is_open, summary=order.summary,
                mail_count=order.mail_count, first_mail_at=order.first_mail_at,
                last_mail_at=order.last_mail_at, numbers=numbers,
                reasons=tuple(reasons), latest_mails=latest, rank=best_rank,
            )
        )

    ranked.sort(key=lambda c: (c.rank, -(c.last_mail_at or now).timestamp()))
    return ranked[:_MAX_CANDIDATES]


def _within_days(value: datetime | None, now: datetime, days: int) -> bool:
    if value is None:
        return False
    return (now - value) <= timedelta(days=days)


def _within_hours(value: datetime | None, now: datetime, hours: int) -> bool:
    if value is None:
        return False
    return (now - value) <= timedelta(hours=hours)


async def _load_identifiers(
    session: AsyncSession, order_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[tuple[str, str, str]]]:
    result = await session.execute(
        select(
            OrderIdentifier.order_id, OrderIdentifier.kind,
            OrderIdentifier.value, OrderIdentifier.value_norm,
        ).where(OrderIdentifier.order_id.in_(order_ids))
    )
    out: dict[uuid.UUID, list[tuple[str, str, str]]] = defaultdict(list)
    for order_id, kind, value, value_norm in result.all():
        out[order_id].append((kind, value, value_norm))
    return out


@dataclass(frozen=True)
class _OrderMails:
    froms: tuple[str, ...]
    latest: tuple[CandidateMail, ...]


async def _load_mails(
    session: AsyncSession, order_ids: list[uuid.UUID],
) -> dict[uuid.UUID, _OrderMails]:
    """Per order: every sender address (for the domain rule) and the
    newest 3 mails, oldest first, for the candidate text (orders/
    prompts.py)."""
    result = await session.execute(
        select(OrderMail.order_id, OrderMail.from_addr, OrderMail.subject, OrderMail.received_at)
        .where(OrderMail.order_id.in_(order_ids))
        .order_by(OrderMail.order_id, OrderMail.received_at.desc())
    )
    froms_by_order: dict[uuid.UUID, list[str]] = defaultdict(list)
    latest_desc_by_order: dict[uuid.UUID, list[CandidateMail]] = defaultdict(list)
    for order_id, from_addr, subject, received_at in result.all():
        froms_by_order[order_id].append(from_addr or "")
        latest = latest_desc_by_order[order_id]
        if len(latest) < 3:
            latest.append(
                CandidateMail(
                    received_at=received_at, from_addr=from_addr or "", subject=subject or "",
                )
            )
    return {
        order_id: _OrderMails(
            froms=tuple(froms_by_order[order_id]),
            latest=tuple(reversed(latest_desc_by_order[order_id])),
        )
        for order_id in froms_by_order
    }


__all__ = [
    "Candidate",
    "CandidateMail",
    "find_candidates",
    "is_valid_identifier",
    "normalize_identifier",
]
