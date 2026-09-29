"""
OrderLookup: the two bypass rules the pipeline stage checks before the
first filter's patterns -- a support reply or a bare "Re:" answer must
reach the register even when its own subject says nothing.

Built once per pipeline run (pipeline/context.py's RunContext), the same
way NeighborService is -- scoped to nothing (the register spans every
enabled account), reading fresh on each call rather than caching.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Order, OrderIdentifier, OrderMail
from mail_verdict.orders.candidates import normalize_identifier

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

_NUMBER_WINDOW_DAYS = 365


class OrderLookup:
    """Whether a mail is already known to the register, independent of
    the first filter's patterns."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def known(
        self, *, account_id: uuid.UUID, thread_id: uuid.UUID | None, text: str,
    ) -> str | None:
        """
        Args:
            account_id: The mail's account
            thread_id: The mail's thread, if any
            text: Subject and raw body, for the number search -- the same
                text orders/candidates.py's number match scans

        Returns:
            "thread" if the mail's thread already belongs to an order,
            "number" if its text contains a number an order of the last
            365 days holds, otherwise None
        """
        async with self._db.session() as session:
            if thread_id is not None and await self._thread_known(
                session, account_id=account_id, thread_id=thread_id,
            ):
                return "thread"
            if await self._number_known(session, text=text):
                return "number"
        return None

    @staticmethod
    async def _thread_known(
        session: AsyncSession, *, account_id: uuid.UUID, thread_id: uuid.UUID,
    ) -> bool:
        result = await session.execute(
            select(OrderMail.id)
            .where(OrderMail.account_id == account_id, OrderMail.thread_id == thread_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def _number_known(session: AsyncSession, *, text: str) -> bool:
        haystack_norm = normalize_identifier(text.upper())
        if not haystack_norm:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=_NUMBER_WINDOW_DAYS)
        result = await session.execute(
            select(OrderIdentifier.value_norm)
            .join(Order, Order.id == OrderIdentifier.order_id)
            .where(Order.last_mail_at.is_not(None), Order.last_mail_at >= cutoff)
        )
        for (value_norm,) in result.all():
            if value_norm and len(value_norm) >= 5 and value_norm in haystack_norm:
                return True
        return False
