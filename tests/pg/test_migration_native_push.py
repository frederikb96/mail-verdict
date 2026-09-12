"""
0032 against the state the previous release reaches: browser subscriptions
with and without a folder scope, a label and a recorded failure. They must
come through untouched as webpush rows with nothing muted; the new checks
and the installation index must be real constraints; and the downgrade must
drop native rows and give the old shape back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_PREVIOUS = "0031_unified_views"

_SCOPED = uuid.UUID("77777777-7777-7777-7777-777777777771")
_PLAIN = uuid.UUID("77777777-7777-7777-7777-777777777772")
_FOLDER = uuid.UUID("88888888-8888-8888-8888-888888888881")


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


async def _downgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, _alembic_config(url), revision)


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to the revision before this one, holding
    two browser subscriptions in the shape that revision produces."""
    name = f"nativepush_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _POSTIMAP_STUBS.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, _PREVIOUS)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO push_subscriptions "
                "(id, endpoint, p256dh, auth, label, alert_folder_ids, reminders_enabled, "
                "last_seen_at, failed_at) VALUES "
                "(:scoped, 'https://push.example/scoped', 'p1', 'a1', 'Laptop', "
                "ARRAY[CAST(:folder AS uuid)], false, now(), now()), "
                "(:plain, 'https://push.example/plain', 'p2', 'a2', NULL, NULL, true, NULL, NULL)"
            ),
            {"scoped": _SCOPED, "plain": _PLAIN, "folder": _FOLDER},
        )
    await engine.dispose()
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


_NATIVE_INSERT = (
    "INSERT INTO push_subscriptions "
    "(transport, installation_id, relay_url, encrypted_relay_ticket, encrypted_content_key) "
    "VALUES ('apns', :installation, 'https://relay.example', :ticket, '\\x01'::bytea)"
)


@pytest.mark.asyncio
async def test_existing_browser_rows_come_through_as_webpush_with_nothing_muted(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, "head")

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.connect() as conn:
            rows = {
                row.id: row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, transport, endpoint, p256dh, auth, label, "
                            "alert_folder_ids, reminders_enabled, failed_at, muted_channels, "
                            "installation_id FROM push_subscriptions"
                        )
                    )
                ).all()
            }
    finally:
        await engine.dispose()

    assert set(rows) == {_SCOPED, _PLAIN}
    scoped, plain = rows[_SCOPED], rows[_PLAIN]
    assert scoped.transport == plain.transport == "webpush"
    assert scoped.muted_channels == plain.muted_channels == []
    assert scoped.installation_id is None
    assert (scoped.endpoint, scoped.p256dh, scoped.auth, scoped.label) == (
        "https://push.example/scoped", "p1", "a1", "Laptop",
    )
    assert scoped.alert_folder_ids == [_FOLDER]
    assert scoped.reminders_enabled is False and scoped.failed_at is not None
    assert plain.alert_folder_ids is None and plain.label is None


@pytest.mark.asyncio
async def test_each_transport_must_carry_its_own_fields(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, "head")

    engine = create_async_engine(db_at_previous)
    installation = uuid.uuid4()
    refused = [
        (
            "a native row with no ticket",
            "INSERT INTO push_subscriptions (transport, installation_id, relay_url, "
            "encrypted_content_key) VALUES ('apns', gen_random_uuid(), 'https://r', '\\x01')",
            {},
        ),
        (
            "a browser row with no endpoint",
            "INSERT INTO push_subscriptions (transport, p256dh, auth) VALUES ('webpush', 'p', 'a')",
            {},
        ),
        (
            "an unknown transport",
            "INSERT INTO push_subscriptions (transport, endpoint, p256dh, auth) "
            "VALUES ('fcm', 'https://x', 'p', 'a')",
            {},
        ),
        (
            "a second device under one installation id",
            _NATIVE_INSERT, {"installation": installation, "ticket": b"second"},
        ),
    ]
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(_NATIVE_INSERT), {"installation": installation, "ticket": b"first"},
            )
        for label, statement, params in refused:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(text(statement), params)
                pytest.fail(f"accepted {label}")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_native_rows_and_restores_the_previous_shape(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, "head")
    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(_NATIVE_INSERT), {"installation": uuid.uuid4(), "ticket": b"t"},
            )

        await _downgrade(db_at_previous, _PREVIOUS)

        async with engine.connect() as conn:
            ids = set((await conn.execute(text("SELECT id FROM push_subscriptions"))).scalars())
            columns = {
                row.column_name: row.is_nullable
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_name = 'push_subscriptions'"
                        )
                    )
                ).all()
            }
        assert ids == {_SCOPED, _PLAIN}
        assert "transport" not in columns and "muted_channels" not in columns
        assert columns["endpoint"] == "NO"
    finally:
        await engine.dispose()

    await _upgrade(db_at_previous, "head")
