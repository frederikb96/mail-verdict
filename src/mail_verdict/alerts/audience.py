"""
Who an alert is for.

An alert about an account's mail, a send or a webhook belongs to whoever
owns that account: its user_id is stamped from account_owners as the row is
written, and moves with the account when an admin gives it to someone else
(reassign_account_alerts). An alert about the instance itself -- no account,
no user -- is the admins'. An alert of an account nobody owns reaches no one.

The audience is named the way the live stream names its keys
(auth/scope.py): a user's id as a string, or ADMIN_RING_KEY. The SSE event,
the push fan-out and the read-sync push all address an alert by it, and
alert_visible_to() is the same rule for a read.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, false, or_, select, update

from mail_verdict.auth.scope import ADMIN_RING_KEY, Scope
from mail_verdict.database.models import AccountOwner, Alert

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mail_verdict.database.connection import DatabaseConnection


def audience_of(user_id: uuid.UUID | None, account_id: uuid.UUID | None) -> str | None:
    """The audience of an alert with this owner and account, or None when
    nobody receives it (an account without an owner)."""
    if user_id is not None:
        return str(user_id)
    return ADMIN_RING_KEY if account_id is None else None


def alert_audience(alert: Alert) -> str | None:
    """audience_of() for one alert row."""
    return audience_of(alert.user_id, alert.account_id)


def scope_audiences(scope: Scope) -> frozenset[str]:
    """The audiences whose alerts a scope reads."""
    audiences = {ADMIN_RING_KEY} if scope.is_admin else set()
    if scope.user_id is not None:
        audiences.add(str(scope.user_id))
    return frozenset(audiences)


def alert_visible_to(scope: Scope) -> ColumnElement[bool]:
    """SQL: the alert is the caller's -- theirs by user_id, or, for an
    admin, one about the instance itself."""
    own = Alert.user_id == scope.user_id if scope.user_id is not None else false()
    if not scope.is_admin:
        return own
    return or_(own, and_(Alert.user_id.is_(None), Alert.account_id.is_(None)))


def account_owner_of(account_id: uuid.UUID) -> ColumnElement[uuid.UUID]:
    """SQL: the owner of an account, for stamping an alert's user_id in the
    statement that writes it."""
    return (
        select(AccountOwner.user_id)
        .where(AccountOwner.account_id == account_id)
        .scalar_subquery()
    )


async def audiences_of(db: DatabaseConnection, alert_ids: Collection[uuid.UUID]) -> set[str]:
    """The audiences of these alerts."""
    if not alert_ids:
        return set()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Alert.user_id, Alert.account_id)
                .where(Alert.id.in_(list(alert_ids)))
                .distinct()
            )
        ).all()
    return {
        audience for audience in (audience_of(row.user_id, row.account_id) for row in rows)
        if audience is not None
    }


async def reassign_account_alerts(
    session: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID,
) -> None:
    """Hand an account's alerts to its new owner, in the caller's transaction."""
    await session.execute(
        update(Alert).where(Alert.account_id == account_id).values(user_id=user_id)
    )
