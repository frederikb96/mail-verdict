"""
0050_users_and_ownership against the rows a single-user deployment holds.

Before this revision every row belonged to the one person using the
instance. After it, every account, DAV account and per-user row belongs to
the owner row the revision creates -- except an alert about the instance
itself, which belongs to nobody -- and that person's own settings are
copied into user_settings. An empty database would prove none of it: the
backfills would have nothing to do.
"""

from __future__ import annotations

import asyncio
import json
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

_PREVIOUS = "0049_llm_usage"
_REVISION = "0050_users_and_ownership"
_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")

_ACCOUNTS = [uuid.uuid4(), uuid.uuid4()]
_DAV_ACCOUNT = uuid.uuid4()

# The tables whose every seeded row must end up the owner's.
_OWNED_BY_USER = (
    "push_subscriptions", "unified_views", "orders", "pipeline_revisions",
    "message_action_submissions", "outbox_submissions", "llm_usage",
)


async def _execute(url: str, *statements: tuple[str, dict[str, Any]]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for statement, params in statements:
                await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _rows(url: str, query: str) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(query))).all())
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database at 0049 with PostIMAP's tables stubbed, dav_accounts
    among them, and a row in every table 0050 touches."""
    name = f"users0050_{uuid.uuid4().hex[:12]}"
    admin_url = postgres_url.rsplit("/", 1)[0]
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    url = f"{admin_url}/{name}"
    stubs = _POSTIMAP_STUBS + "CREATE TABLE dav_accounts (id uuid PRIMARY KEY, name text);"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in stubs.split(";"))):
            await conn.execute(text(statement))
    await engine.dispose()

    await _upgrade(url, _PREVIOUS)
    await _seed(url)
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _seed(url: str) -> None:
    """What a 7.x deployment holds: two mail accounts, a DAV account, and a
    row of every kind that becomes a user's."""
    statements: list[tuple[str, dict[str, Any]]] = [
        ("INSERT INTO accounts (id, name) VALUES (:id, :name)", {"id": a, "name": f"acct-{i}"})
        for i, a in enumerate(_ACCOUNTS)
    ]
    statements += [
        ("INSERT INTO dav_accounts (id, name) VALUES (:id, 'cloud')", {"id": _DAV_ACCOUNT}),
        (
            "INSERT INTO push_subscriptions (id, transport, endpoint, p256dh, auth) "
            "VALUES (:id, 'webpush', 'https://push.example/1', 'k', 'a')",
            {"id": uuid.uuid4()},
        ),
        (
            "INSERT INTO alerts (id, kind, deliver_at, dedupe_key, account_id) "
            "VALUES (:id, 'mail', now(), 'mail:1', :account)",
            {"id": uuid.uuid4(), "account": _ACCOUNTS[0]},
        ),
        (
            "INSERT INTO alerts (id, kind, deliver_at, dedupe_key, object_id) "
            "VALUES (:id, 'reminder', now(), 'reminder:1', :object)",
            {"id": uuid.uuid4(), "object": uuid.uuid4()},
        ),
        (
            "INSERT INTO alerts (id, kind, deliver_at, dedupe_key) "
            "VALUES (:id, 'provider_unavailable', now(), 'provider:openai')",
            {"id": uuid.uuid4()},
        ),
        ("INSERT INTO unified_views (id, name) VALUES (:id, 'Everything')", {"id": uuid.uuid4()}),
        ("INSERT INTO orders (id) VALUES (:id)", {"id": uuid.uuid4()}),
        (
            "INSERT INTO pipeline_revisions (document, note) VALUES ('{}'::jsonb, 'seeded')",
            {},
        ),
        (
            "INSERT INTO message_action_submissions (idempotency_key, fingerprint, claim_token) "
            "VALUES (:key, 'f', :claim)",
            {"key": uuid.uuid4(), "claim": uuid.uuid4()},
        ),
        (
            "INSERT INTO outbox_submissions (idempotency_key, kind, target_id) "
            "VALUES (:key, 'send', :target)",
            {"key": uuid.uuid4(), "target": uuid.uuid4()},
        ),
        (
            "INSERT INTO llm_usage (purpose, provider, model, input_tokens, cache_read_tokens, "
            "cache_write_tokens, output_tokens) VALUES ('verdict', 'openai', 'm', 1, 0, 0, 1)",
            {},
        ),
        ("UPDATE calendar_links_revision SET revision = 7", {}),
    ]
    settings = {
        "mail": {"bell_badge_counts_new_mail": False},
        "calendar": {"default_reminder_minutes": 30},
        "outbox": {"undo_send_seconds": 10},
        "orders": {"language": "German", "auto_close_days": 45, "decide_model": "m-1"},
        "account_order": {"order": [str(a) for a in reversed(_ACCOUNTS)]},
        "ai": {"model": "classifier"},
    }
    statements += [
        (
            "INSERT INTO settings (category, data) VALUES (:c, CAST(:d AS jsonb))",
            {"c": category, "d": json.dumps(data)},
        )
        for category, data in settings.items()
    ]
    await _execute(url, *statements)


@pytest.mark.asyncio
async def test_every_account_and_dav_account_belongs_to_the_owner(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    owners = dict(await _rows(db_at_previous, "SELECT account_id, user_id FROM account_owners"))
    dav_owners = dict(
        await _rows(db_at_previous, "SELECT dav_account_id, user_id FROM dav_account_owners")
    )
    users = await _rows(db_at_previous, "SELECT id, role, disabled FROM users")

    assert owners == {account: _OWNER for account in _ACCOUNTS}
    assert dav_owners == {_DAV_ACCOUNT: _OWNER}
    assert users == [(_OWNER, "admin", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _OWNED_BY_USER)
async def test_every_row_of_a_user_owned_table_belongs_to_the_owner(
    db_at_previous: str, table: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    owners = {row[0] for row in await _rows(db_at_previous, f"SELECT user_id FROM {table}")}

    assert owners == {_OWNER}


@pytest.mark.asyncio
async def test_alerts_belong_to_the_owner_except_the_instances_own(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    owners = dict(await _rows(db_at_previous, "SELECT kind, user_id FROM alerts"))

    assert owners == {"mail": _OWNER, "reminder": _OWNER, "provider_unavailable": None}


@pytest.mark.asyncio
async def test_the_calendar_links_revision_carries_over_as_the_owners(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    rows = await _rows(db_at_previous, "SELECT user_id, revision FROM calendar_links_revision")

    assert rows == [(_OWNER, 7)]


@pytest.mark.asyncio
async def test_the_owners_own_settings_are_copied_and_the_instances_are_not(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    copied = {
        category: data
        for _, category, data in await _rows(
            db_at_previous, "SELECT user_id, category, data FROM user_settings",
        )
    }

    assert copied == {
        "mail": {
            "bell_badge_counts_new_mail": False,
            "account_order": [str(a) for a in reversed(_ACCOUNTS)],
        },
        "calendar": {"default_reminder_minutes": 30},
        "outbox": {"undo_send_seconds": 10},
        "orders": {"language": "German", "auto_close_days": 45},
    }


@pytest.mark.asyncio
async def test_a_unified_view_name_is_unique_per_user_not_per_instance(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)
    other = uuid.uuid4()

    await _execute(
        db_at_previous,
        ("INSERT INTO users (id, display_name) VALUES (:id, 'Other')", {"id": other}),
        (
            "INSERT INTO unified_views (id, name, user_id) VALUES (:id, 'Everything', :user)",
            {"id": uuid.uuid4(), "user": other},
        ),
    )

    names = await _rows(
        db_at_previous, "SELECT user_id FROM unified_views WHERE name = 'Everything'",
    )
    assert {row[0] for row in names} == {_OWNER, other}


@pytest.mark.asyncio
async def test_views_no_user_owns_yet_still_collide_on_name(db_at_previous: str) -> None:
    """A view written without a user (by code that does not stamp one yet)
    is unique by name among the others like it, as every view was before."""
    await _upgrade(db_at_previous, _REVISION)
    insert = "INSERT INTO unified_views (id, name) VALUES (:id, 'Unowned')"

    await _execute(db_at_previous, (insert, {"id": uuid.uuid4()}))
    with pytest.raises(IntegrityError):
        await _execute(db_at_previous, (insert, {"id": uuid.uuid4()}))


@pytest.mark.asyncio
async def test_downgrading_restores_the_single_counter(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    assert await _rows(db_at_previous, "SELECT revision FROM calendar_links_revision") == [(7,)]
    assert await _rows(db_at_previous, "SELECT to_regclass('users')") == [(None,)]
