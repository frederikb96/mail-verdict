"""
alerts/ownerless.py against a real database: a mail or DAV account without
an owner raises one alert for the admins, and the alert is dismissed once
the account has an owner.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from mail_verdict.alerts.ownerless import ALERT_KIND, DEDUPE_PREFIX, raise_ownerless_alerts_once
from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Alert
from mail_verdict.database.repository import AlertRepository
from tests.helpers.auth import owner_scope

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _no_alerts_left(migrated_db: DatabaseConnection) -> AsyncIterator[None]:
    """A pass alerts on every ownerless account in the shared database, other
    modules' seeds included; none of those alerts may outlive this module's
    tests and change what a later module counts."""
    yield
    async with migrated_db.session() as session:
        await session.execute(delete(Alert).where(Alert.kind == ALERT_KIND))


async def _seed_mail_account(db: DatabaseConnection) -> uuid.UUID:
    """An inactive account (PostIMAP leaves those alone) with no owner row."""
    account_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO accounts (id, name, imap_host, imap_port, imap_user, imap_password, "
                "is_active) VALUES (:id, :name, 'imap.example.com', 993, 'u@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'), false)"
            ),
            {"id": account_id, "name": f"stray-{account_id}"},
        )
    return account_id


async def _alerts(db: DatabaseConnection, account_id: uuid.UUID) -> list[Alert]:
    async with db.session() as session:
        return list(
            (
                await session.execute(
                    select(Alert).where(
                        Alert.kind == ALERT_KIND,
                        Alert.dedupe_key == f"{DEDUPE_PREFIX}{account_id}",
                    )
                )
            ).scalars().all()
        )


async def test_an_account_without_an_owner_raises_one_alert_for_the_admins(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = await _seed_mail_account(migrated_db)

    await raise_ownerless_alerts_once(migrated_db, None, None)
    await raise_ownerless_alerts_once(migrated_db, None, None)

    [alert] = await _alerts(migrated_db, account_id)
    assert alert.account_id is None and alert.user_id is None
    assert alert.body is not None and f"stray-{account_id}" in alert.body
    assert alert.delivered_at is not None and alert.dismissed_at is None
    owners_alerts = await AlertRepository(migrated_db).list_recent(owner_scope(), limit=500)
    assert alert.id in {a.id for a in owners_alerts}


async def test_the_alert_is_dismissed_once_the_account_has_an_owner(
    migrated_db: DatabaseConnection,
) -> None:
    account_id = await _seed_mail_account(migrated_db)
    await raise_ownerless_alerts_once(migrated_db, None, None)
    async with migrated_db.session() as session:
        await session.execute(
            text("INSERT INTO account_owners (account_id, user_id) VALUES (:a, :u)"),
            {"a": account_id, "u": OWNER_USER_ID},
        )

    await raise_ownerless_alerts_once(migrated_db, None, None)

    [alert] = await _alerts(migrated_db, account_id)
    assert alert.dismissed_at is not None


async def test_a_dav_account_without_an_owner_is_named_too(
    migrated_db: DatabaseConnection,
) -> None:
    dav_account_id = uuid.uuid4()
    async with migrated_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO dav_accounts (id, name, url, username, password, is_active) "
                "VALUES (:id, :name, 'https://dav.example.com/', 'u@example.com', "
                "'\\x00' || convert_to('pw', 'UTF8'), false)"
            ),
            {"id": dav_account_id, "name": f"stray-dav-{dav_account_id}"},
        )

    await raise_ownerless_alerts_once(migrated_db, None, None)

    [alert] = await _alerts(migrated_db, dav_account_id)
    assert alert.body is not None and "calendars and contacts" in alert.body
