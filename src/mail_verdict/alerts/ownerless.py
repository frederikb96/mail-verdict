"""
Raising an alert for an account nobody owns.

An account with no account_owners row belongs to nobody: the pipeline skips
its mail, its alerts reach no one, and no user sees it. MailVerdict writes
the owner row in the same transaction as every account it creates, so such
an account was created some other way -- directly in PostIMAP, say -- and
nothing else would ever say so.

This pass alerts the admins once per such account, mail or DAV, and
dismisses the alert once the account has an owner or is gone. The alert is
the instance's (no account, no user: alerts/audience.py), since an alert
naming the account would reach its owner, and there is none; the account is
named in the text and in the dedupe key, which is what makes it fire once.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from mail_verdict.alerts.dispatch import deliver_alert
from mail_verdict.alerts.resolve import announce_alerts_dismissed
from mail_verdict.auth.ownership import account_display_name
from mail_verdict.database.models import (
    Account,
    AccountOwner,
    Alert,
    DavAccount,
    DavAccountOwner,
)
from mail_verdict.database.repository import AlertRepository
from mail_verdict.queue.notify import ReconciliationTimer

if TYPE_CHECKING:
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.push.vapid import VapidKeyRepository

logger = logging.getLogger(__name__)

# Distinct from every other ReconciliationTimer's lock key in the process
# (tests/unit/test_lock_keys.py checks them all).
_OWNERLESS_ALERT_LOCK_KEY = 761_035_700

_POLL_INTERVAL_SECONDS = 60.0

ALERT_KIND = "account_unowned"
DEDUPE_PREFIX = "account-unowned:"


async def _ownerless(db: DatabaseConnection) -> dict[str, tuple[str, bool]]:
    """dedupe key -> (display name, is a DAV account) of every account
    without an owner."""
    async with db.session() as session:
        mail = (
            await session.execute(
                select(Account.id, Account.name)
                .outerjoin(AccountOwner, AccountOwner.account_id == Account.id)
                .where(AccountOwner.account_id.is_(None))
            )
        ).all()
        dav = (
            await session.execute(
                select(DavAccount.id, DavAccount.name)
                .outerjoin(DavAccountOwner, DavAccountOwner.dav_account_id == DavAccount.id)
                .where(DavAccountOwner.dav_account_id.is_(None))
            )
        ).all()
    found = {f"{DEDUPE_PREFIX}{row.id}": (account_display_name(row.name), False) for row in mail}
    found.update(
        {f"{DEDUPE_PREFIX}{row.id}": (account_display_name(row.name), True) for row in dav}
    )
    return found


async def _resolve_owned(db: DatabaseConnection, ownerless: set[str]) -> list[uuid.UUID]:
    """Dismiss the open alerts of accounts that have an owner again or are
    gone. Returns the ids dismissed."""
    async with db.session() as session:
        open_alerts = (
            await session.execute(
                select(Alert.id, Alert.dedupe_key).where(
                    Alert.kind == ALERT_KIND, Alert.dismissed_at.is_(None),
                )
            )
        ).all()
        owned = [row.id for row in open_alerts if row.dedupe_key not in ownerless]
        if owned:
            await session.execute(
                update(Alert).where(Alert.id.in_(owned)).values(
                    dismissed_at=datetime.now(timezone.utc),
                )
            )
    return owned


async def raise_ownerless_alerts_once(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
) -> None:
    """One pass: dismiss the alerts of accounts that are owned again, then
    alert, once each, for every account without an owner."""
    ownerless = await _ownerless(db)
    resolved = await _resolve_owned(db, set(ownerless))
    if resolved:
        await announce_alerts_dismissed(db, event_ring, resolved)

    repo = AlertRepository(db)
    for dedupe_key, (name, is_dav) in ownerless.items():
        what = "calendars and contacts" if is_dav else "mail"
        alert = await repo.create_instance_alert(
            kind=ALERT_KIND,
            dedupe_key=dedupe_key,
            title="Account without an owner",
            body=(
                f"“{name}” belongs to nobody, so nobody sees its {what}"
                + ("" if is_dav else ", and it is not classified and raises no alerts")
                + ". Give it to someone in Administration › Users."
            ),
        )
        if alert is None:
            continue
        logger.warning("An account has no owner", extra={"dedupe_key": dedupe_key})
        await deliver_alert(db, event_ring, vapid_repo, alert)


def build_ownerless_alert_timer(
    db: DatabaseConnection,
    event_ring: EventRing | None,
    vapid_repo: VapidKeyRepository | None,
) -> ReconciliationTimer:
    """The advisory-locked periodic pass that raises ownerless-account
    alerts -- one per process, safe with more than one replica."""

    async def _callback() -> None:
        await raise_ownerless_alerts_once(db, event_ring, vapid_repo)

    return ReconciliationTimer(db, _OWNERLESS_ALERT_LOCK_KEY, _callback, _POLL_INTERVAL_SECONDS)
