"""
0053_user_id_not_null against rows written without a user: an order whose
mail came from an account another user owns, an order with no mail yet, a
file in each and one waiting for an order, and a row of every other table
that becomes NOT NULL -- among them a unified view whose name the owner
already uses. Each row gets the user it belongs to, and none can be written
without one afterwards.
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

_PREVIOUS = "0052_folder_pipeline_switch"
_REVISION = "0053_user_id_not_null"
_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")

_OTHER_USER = uuid.uuid4()
_OTHERS_ACCOUNT = uuid.uuid4()
_ORDER_OF_OTHERS_MAIL = uuid.uuid4()
_ORDER_WITHOUT_MAIL = uuid.uuid4()
_FILE_OF_OTHERS_ORDER = uuid.uuid4()
_FILE_WAITING = uuid.uuid4()
_OWNERS_VIEW = uuid.uuid4()
_CLASHING_VIEW = uuid.uuid4()
_LONE_VIEW = uuid.uuid4()

# Every table whose user_id becomes NOT NULL, with a row of it as written
# before its writer stamped a user.
_NOT_NULL = {
    "push_subscriptions": (
        "INSERT INTO push_subscriptions (id, transport, endpoint, p256dh, auth) "
        "VALUES (:id, 'webpush', :endpoint, 'k', 'a')"
    ),
    "pipeline_revisions": (
        "INSERT INTO pipeline_revisions (document, note) VALUES ('{}'::jsonb, :endpoint)"
    ),
    "message_action_submissions": (
        "INSERT INTO message_action_submissions (idempotency_key, fingerprint, claim_token) "
        "VALUES (:id, 'f', :id)"
    ),
    "outbox_submissions": (
        "INSERT INTO outbox_submissions (idempotency_key, kind, target_id) "
        "VALUES (:id, 'send', :id)"
    ),
    "unified_views": "INSERT INTO unified_views (id, name) VALUES (:id, :endpoint)",
    "orders": "INSERT INTO orders (id) VALUES (:id)",
    "order_files": (
        "INSERT INTO order_files (id, filename, size_bytes, sha256, data) "
        "VALUES (:id, 'f.pdf', 1, :endpoint, '\\x00')"
    ),
}

_FILE = (
    "INSERT INTO order_files (id, order_id, filename, size_bytes, sha256, data) "
    "VALUES (:id, :order, 'f.pdf', 1, :sha, '\\x00')"
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
    """A throwaway database at 0052 holding rows with no user."""
    name = f"usernotnull0053_{uuid.uuid4().hex[:12]}"
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
    await _execute(
        url,
        ("INSERT INTO users (id, display_name) VALUES (:id, 'Other')", {"id": _OTHER_USER}),
        ("INSERT INTO accounts (id, name) VALUES (:id, 'theirs')", {"id": _OTHERS_ACCOUNT}),
        (
            "INSERT INTO account_owners (account_id, user_id) VALUES (:a, :u)",
            {"a": _OTHERS_ACCOUNT, "u": _OTHER_USER},
        ),
        ("INSERT INTO orders (id) VALUES (:id)", {"id": _ORDER_OF_OTHERS_MAIL}),
        ("INSERT INTO orders (id) VALUES (:id)", {"id": _ORDER_WITHOUT_MAIL}),
        (
            "INSERT INTO order_mails (order_id, account_id, msg_key, received_at, attached_by) "
            "VALUES (:order, :account, '<m@x>', now(), 'ai')",
            {"order": _ORDER_OF_OTHERS_MAIL, "account": _OTHERS_ACCOUNT},
        ),
        (_FILE, {"id": _FILE_OF_OTHERS_ORDER, "order": _ORDER_OF_OTHERS_MAIL, "sha": "a"}),
        (_FILE, {"id": _FILE_WAITING, "order": None, "sha": "b"}),
        (
            "INSERT INTO unified_views (id, name, user_id) VALUES (:id, 'Everything', :owner)",
            {"id": _OWNERS_VIEW, "owner": _OWNER},
        ),
        ("INSERT INTO unified_views (id, name) VALUES (:id, 'Everything')", {"id": _CLASHING_VIEW}),
        ("INSERT INTO unified_views (id, name) VALUES (:id, 'Lone')", {"id": _LONE_VIEW}),
        (_NOT_NULL["push_subscriptions"], {"id": uuid.uuid4(), "endpoint": "https://p.example/1"}),
        (_NOT_NULL["pipeline_revisions"], {"endpoint": "seeded"}),
        (_NOT_NULL["message_action_submissions"], {"id": uuid.uuid4()}),
        (_NOT_NULL["outbox_submissions"], {"id": uuid.uuid4()}),
    )
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_an_order_takes_the_owner_of_its_mail_and_a_file_its_orders_user(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    orders = dict(await _rows(db_at_previous, "SELECT id, user_id FROM orders"))
    files = dict(await _rows(db_at_previous, "SELECT id, user_id FROM order_files"))

    assert orders == {_ORDER_OF_OTHERS_MAIL: _OTHER_USER, _ORDER_WITHOUT_MAIL: _OWNER}
    assert files == {_FILE_OF_OTHERS_ORDER: _OTHER_USER, _FILE_WAITING: _OWNER}


@pytest.mark.asyncio
async def test_a_view_given_to_the_owner_keeps_its_name_unless_the_owner_has_it(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    views = {
        row.id: (row.name, row.user_id)
        for row in await _rows(db_at_previous, "SELECT id, name, user_id FROM unified_views")
    }

    assert views == {
        _OWNERS_VIEW: ("Everything", _OWNER),
        _CLASHING_VIEW: (f"Everything ({str(_CLASHING_VIEW)[:8]})", _OWNER),
        _LONE_VIEW: ("Lone", _OWNER),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    ["push_subscriptions", "pipeline_revisions", "message_action_submissions",
     "outbox_submissions"],
)
async def test_any_other_row_without_a_user_goes_to_the_owner(
    db_at_previous: str, table: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    owners = {row[0] for row in await _rows(db_at_previous, f"SELECT user_id FROM {table}")}

    assert owners == {_OWNER}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(_NOT_NULL))
async def test_a_row_without_a_user_is_refused_afterwards(
    db_at_previous: str, table: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    with pytest.raises(IntegrityError, match="user_id"):
        await _execute(
            db_at_previous,
            (_NOT_NULL[table], {"id": uuid.uuid4(), "endpoint": f"x-{uuid.uuid4()}"}),
        )


@pytest.mark.asyncio
async def test_alerts_and_model_usage_still_take_rows_that_belong_to_nobody(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await _execute(
        db_at_previous,
        (
            "INSERT INTO alerts (id, kind, deliver_at, dedupe_key) "
            "VALUES (:id, 'provider_unavailable', now(), 'provider:openai')",
            {"id": uuid.uuid4()},
        ),
        (
            "INSERT INTO llm_usage (purpose, provider, model, input_tokens, "
            "cache_read_tokens, cache_write_tokens, output_tokens) "
            "VALUES ('verdict', 'openai', 'm', 1, 0, 0, 1)",
            {},
        ),
    )


@pytest.mark.asyncio
async def test_downgrading_makes_the_columns_nullable_again(db_at_previous: str) -> None:
    await _upgrade(db_at_previous, _REVISION)

    await asyncio.to_thread(command.downgrade, _alembic_config(db_at_previous), _PREVIOUS)

    nullable = dict(
        await _rows(
            db_at_previous,
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE column_name = 'user_id' AND table_name IN ("
            + ", ".join(f"'{table}'" for table in _NOT_NULL) + ")",
        )
    )
    assert nullable == dict.fromkeys(_NOT_NULL, "YES")
