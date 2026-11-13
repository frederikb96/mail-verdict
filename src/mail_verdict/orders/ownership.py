"""
Who an order belongs to, and what follows an order's owner changing.

An order is stamped with the owner of the account its first mail came from
(orders/worker.py) and holds only that user's mail. When an admin gives an
account to another user, mail of that account must stop being bundled into
the previous owner's orders: `reassign_account` moves what can move whole and
takes the account's mail out of the rest.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, text, update

from mail_verdict.database.models import Order, OrderDocument, OrderMail
from mail_verdict.orders import repository
from mail_verdict.orders.announce import OrderChange
from mail_verdict.orders.worker import ORDERS_LOCK_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def reassign_account(
    session: AsyncSession, account_id: uuid.UUID, new_user_id: uuid.UUID,
) -> list[OrderChange]:
    """
    Settle the orders that hold mail of an account whose owner is now
    `new_user_id`.

    An order whose mail all comes from this account, and that holds no file
    its owner added by hand, moves to the new owner whole -- text, facts and
    documents -- so their history is there at once. In any other order (mail
    of the previous owner's other accounts, or their files) only this
    account's mail is taken out, with its documents; an order left empty is
    deleted, the rest is rewritten. The account's finished jobs go, so a
    catch-up of the new owner can read that mail again; jobs still waiting
    run for the new owner.

    The caller commits; the orders lock is taken here.

    Returns:
        (user_id, order_id, change) for each order to announce
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ORDERS_LOCK_KEY})
    rows = (
        await session.execute(
            text(
                """
                SELECT orders.id, orders.user_id, orders.file_count,
                       count(*) AS mails,
                       count(*) FILTER (WHERE order_mails.account_id = :account) AS from_account
                FROM orders
                JOIN order_mails ON order_mails.order_id = orders.id
                WHERE orders.id IN (SELECT order_id FROM order_mails WHERE account_id = :account)
                GROUP BY orders.id
                """
            ),
            {"account": account_id},
        )
    ).all()

    changes: list[OrderChange] = []
    for order_id, previous_user_id, file_count, mails, from_account in rows:
        if previous_user_id == new_user_id:
            continue
        if mails == from_account and file_count == 0:
            await session.execute(
                update(Order).where(Order.id == order_id).values(user_id=new_user_id)
            )
            if previous_user_id is not None:
                changes.append((previous_user_id, order_id, "deleted"))
            changes.append((new_user_id, order_id, "updated"))
            continue
        await session.execute(
            delete(OrderDocument).where(
                OrderDocument.order_id == order_id, OrderDocument.source == "mail",
                OrderDocument.account_id == account_id,
            )
        )
        await session.execute(
            delete(OrderMail).where(
                OrderMail.order_id == order_id, OrderMail.account_id == account_id,
            )
        )
        remaining = await repository.recompute_aggregates(session, order_id)
        deleted = remaining == 0 and await repository.delete_order_if_empty(session, order_id)
        if not deleted:
            await repository.mark_text_stale(session, order_id)
            await repository.enqueue_write_job(session, order_id, priority=50)
        if previous_user_id is not None:
            changes.append((previous_user_id, order_id, "deleted" if deleted else "updated"))

    await session.execute(
        text(
            """
            DELETE FROM order_jobs
            WHERE kind = 'mail' AND account_id = :account AND outcome IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM order_mails
                WHERE order_mails.account_id = order_jobs.account_id
                  AND order_mails.msg_key = order_jobs.msg_key
              )
            """
        ),
        {"account": account_id},
    )
    return changes
