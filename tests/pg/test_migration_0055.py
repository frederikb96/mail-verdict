"""
0055_native_push_transport against the push_subscriptions rows 0054 holds:
an iPhone registered as `apns` beside a browser. The phone becomes `native`,
the browser stays `webpush`, `apns` is refused afterwards, and a downgrade
gives the phone its old name back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config, _upgrade

_PREVIOUS = "0054_sign_ins"
_REVISION = "0055_native_push_transport"
_OWNER = "00000000-0000-0000-0000-000000000001"
_PHONE = uuid.uuid4()
_BROWSER = uuid.uuid4()

_PHONE_ROW = (
    "INSERT INTO push_subscriptions (id, transport, installation_id, relay_url, "
    "encrypted_relay_ticket, encrypted_content_key, user_id) "
    "VALUES (:id, :transport, gen_random_uuid(), 'https://relay.example', '\\x01'::bytea, "
    "'\\x01'::bytea, :owner)"
)


async def _execute(url: str, statement: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _transports(url: str) -> dict[uuid.UUID, str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id, transport FROM push_subscriptions"))).all()
    finally:
        await engine.dispose()
    return {row.id: row.transport for row in rows}


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database at 0054 holding an `apns` phone and a browser."""
    name = f"native0055_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    stubs = _POSTIMAP_STUBS + (
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS imap_host text;"
        "ALTER TABLE folders ADD COLUMN IF NOT EXISTS special_use text;"
        "CREATE TABLE dav_accounts (id uuid PRIMARY KEY, name text);"
    )
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in stubs.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, _PREVIOUS)
    await _execute(url, _PHONE_ROW, {"id": _PHONE, "transport": "apns", "owner": _OWNER})
    await _execute(
        url,
        "INSERT INTO push_subscriptions (id, transport, endpoint, p256dh, auth, user_id) "
        "VALUES (:id, 'webpush', 'https://push.example/1', 'k', 'a', :owner)",
        {"id": _BROWSER, "owner": _OWNER},
    )
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_a_phone_becomes_native_and_a_browser_stays_webpush(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    assert await _transports(db_at_previous) == {_PHONE: "native", _BROWSER: "webpush"}


@pytest.mark.asyncio
async def test_the_platform_name_is_refused_afterwards(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    with pytest.raises(IntegrityError, match="ck_push_subscriptions_transport"):
        await _execute(
            db_at_previous, _PHONE_ROW, {"id": uuid.uuid4(), "transport": "apns", "owner": _OWNER},
        )
    with pytest.raises(IntegrityError, match="ck_push_subscriptions_native_fields"):
        await _execute(
            db_at_previous,
            "INSERT INTO push_subscriptions (transport, relay_url, user_id) "
            "VALUES ('native', 'https://relay.example', :owner)",
            {"owner": _OWNER},
        )


@pytest.mark.asyncio
async def test_a_downgrade_gives_the_phone_its_old_name_back(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    assert await _transports(db_at_previous) == {_PHONE: "apns", _BROWSER: "webpush"}
