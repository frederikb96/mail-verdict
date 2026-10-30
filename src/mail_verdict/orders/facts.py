"""
The key facts of an order, as a write call states them.

Three kinds of fact live in `order_facts` and are kept differently:
- identifier kinds (order, booking, ticket, invoice, tracking number) are a
  union: a write adds and relabels, never deletes -- they are what later
  mails are matched against;
- display kinds (date, time, amount, place, seat, person, link, other) are
  replaced wholesale by every write;
- code facts point at one decoded barcode of a document. Every barcode a
  document carries has one, whether or not the model mentioned it, and a
  fact whose barcode is gone is removed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, cast

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import OrderFact
from mail_verdict.orders.candidates import is_valid_identifier
from mail_verdict.orders.carriers import CARRIERS
from mail_verdict.orders.prompts import MAX_FACTS
from mail_verdict.orders.repository import IDENTIFIER_FACT_KINDS, upsert_identifier_fact

if TYPE_CHECKING:
    from sqlalchemy import Table

DISPLAY_FACT_KINDS = ("date", "time", "amount", "place", "seat", "person", "link", "other")

_MAX_LABEL_CHARS = 30
_MAX_VALUE_CHARS = 160
_DEFAULT_CODE_LABEL = "Code"


@dataclass(frozen=True)
class WrittenFact:
    kind: str
    label: str
    value: str
    carrier: str | None
    code_ref: str | None


def _clean(value: object, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def parse_written_facts(raw: object) -> list[WrittenFact]:
    """The write answer's fact list, cleaned: lengths cut, an empty value
    dropped (a code needs none), at most `MAX_FACTS` kept."""
    facts: list[WrittenFact] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        value = _clean(item.get("value"), _MAX_VALUE_CHARS)
        if not value and kind != "code":
            continue
        carrier = str(item.get("carrier") or "")
        facts.append(
            WrittenFact(
                kind=kind, label=_clean(item.get("label"), _MAX_LABEL_CHARS), value=value,
                carrier=carrier if carrier in CARRIERS else None,
                code_ref=_clean(item.get("code_ref"), 20) or None,
            )
        )
    return facts[:MAX_FACTS]


def _date_value(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def _barcodes_of(
    session: AsyncSession, order_id: uuid.UUID,
) -> dict[tuple[uuid.UUID, int], tuple[str, str | None]]:
    """(document id, barcode index) -> (format, printable text) for every
    barcode of the order's documents. The crops and payloads stay in the
    row."""
    result = await session.execute(
        text(
            """
            SELECT d.id, (e->>'index')::int AS idx, e->>'format', e->>'text'
            FROM order_documents d CROSS JOIN LATERAL jsonb_array_elements(d.barcodes) e
            WHERE d.order_id = :order_id AND e ? 'index'
            """
        ),
        {"order_id": order_id},
    )
    return {(doc, idx): (fmt, shown) for doc, idx, fmt, shown in result.all()}


async def _upsert_code_fact(
    session: AsyncSession, order_id: uuid.UUID, *, document_id: uuid.UUID, barcode_index: int,
    label: str, value: str, source: str,
) -> uuid.UUID:
    next_position = (
        select(func.coalesce(func.max(OrderFact.position) + 1, 0))
        .where(OrderFact.order_id == order_id)
        .scalar_subquery()
    )
    insert_stmt = pg_insert(OrderFact).values(
        order_id=order_id, kind="code", label=label, value=value, document_id=document_id,
        barcode_index=barcode_index, position=next_position, source=source,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["order_id", "document_id", "barcode_index"],
        index_where=text("document_id IS NOT NULL"),
        set_={"label": label, "value": value, "source": source},
    ).returning(OrderFact.id)
    return uuid.UUID(str((await session.execute(stmt)).scalar_one()))


async def reconcile_code_facts(session: AsyncSession, order_id: uuid.UUID) -> None:
    """Give every barcode of the order's documents a code fact (source
    `code` when none exists yet) and remove code facts whose barcode is gone."""
    barcodes = await _barcodes_of(session, order_id)
    held = await session.execute(
        select(OrderFact.id, OrderFact.document_id, OrderFact.barcode_index).where(
            OrderFact.order_id == order_id, OrderFact.kind == "code",
        )
    )
    present: set[tuple[uuid.UUID, int]] = set()
    stale: list[uuid.UUID] = []
    for fact_id, document_id, barcode_index in held.all():
        if document_id is not None and barcode_index is not None and (
            document_id, barcode_index
        ) in barcodes:
            present.add((document_id, barcode_index))
        else:
            stale.append(fact_id)
    if stale:
        await session.execute(delete(OrderFact).where(OrderFact.id.in_(stale)))
    for (document_id, barcode_index), (_, shown) in sorted(
        barcodes.items(), key=lambda item: (str(item[0][0]), item[0][1]),
    ):
        if (document_id, barcode_index) in present:
            continue
        await _upsert_code_fact(
            session, order_id, document_id=document_id, barcode_index=barcode_index,
            label=_DEFAULT_CODE_LABEL, value=_clean(shown, _MAX_VALUE_CHARS), source="code",
        )


async def apply_written_facts(
    session: AsyncSession, order_id: uuid.UUID, facts: Sequence[WrittenFact],
    code_map: Mapping[str, tuple[uuid.UUID, int]],
) -> None:
    """
    Store a write call's facts, in the call's order, then bring the code
    facts in line with the documents.

    Args:
        facts: The cleaned facts (`parse_written_facts`)
        code_map: Which stored barcode each `d<k>:b<n>` marker of the prompt names
    """
    await session.execute(
        delete(OrderFact).where(
            OrderFact.order_id == order_id, OrderFact.kind.in_(DISPLAY_FACT_KINDS),
            OrderFact.source != "user",
        )
    )
    barcodes = await _barcodes_of(session, order_id)
    stated: list[uuid.UUID] = []
    seen_display: set[tuple[str, str]] = set()
    for fact in facts:
        fact_id: uuid.UUID | None = None
        if fact.kind in IDENTIFIER_FACT_KINDS and is_valid_identifier(fact.value):
            fact_id = await upsert_identifier_fact(
                session, order_id, kind=fact.kind, value=fact.value, label=fact.label or None,
                carrier=fact.carrier if fact.kind == "tracking_number" else None,
                update_existing=True,
            )
        elif fact.kind == "code":
            target = code_map.get(fact.code_ref or "")
            if target is not None and target in barcodes:
                fact_id = await _upsert_code_fact(
                    session, order_id, document_id=target[0], barcode_index=target[1],
                    label=fact.label or _DEFAULT_CODE_LABEL, value=fact.value, source="ai",
                )
        else:
            # A display fact; an identifier of an implausible shape is shown, not matched on.
            kind = "other" if fact.kind in IDENTIFIER_FACT_KINDS else fact.kind
            if (kind, fact.value) not in seen_display and fact.label:
                seen_display.add((kind, fact.value))
                fact_id = uuid.uuid4()
                await session.execute(
                    pg_insert(OrderFact).values(
                        id=fact_id, order_id=order_id, kind=kind, label=fact.label,
                        value=fact.value, position=0, source="ai",
                        date_value=_date_value(fact.value) if kind == "date" else None,
                    )
                )
        if fact_id is not None:
            stated.append(fact_id)

    await reconcile_code_facts(session, order_id)
    await _order_facts(session, order_id, stated)


async def _order_facts(
    session: AsyncSession, order_id: uuid.UUID, stated: Sequence[uuid.UUID],
) -> None:
    """Positions: the facts the call stated in its order, then the rest in
    the order they already had."""
    rest = await session.execute(
        select(OrderFact.id)
        .where(OrderFact.order_id == order_id, OrderFact.id.notin_(list(stated)))
        .order_by(OrderFact.position, OrderFact.created_at, OrderFact.id)
    )
    ordering = list(dict.fromkeys(stated)) + [row[0] for row in rest.all()]
    if not ordering:
        return
    table = cast("Table", OrderFact.__table__)
    await session.execute(
        update(table).where(table.c.id == bindparam("fact_id")).values(
            position=bindparam("new_position"),
        ),
        [{"fact_id": fact_id, "new_position": i} for i, fact_id in enumerate(ordering)],
    )


__all__ = [
    "DISPLAY_FACT_KINDS", "WrittenFact", "apply_written_facts", "parse_written_facts",
    "reconcile_code_facts",
]
