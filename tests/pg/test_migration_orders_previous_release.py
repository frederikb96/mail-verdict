"""
0038_orders against the state the previous release reaches: a real,
multi-stage pipeline revision already in place (classify, a filing rule,
move-spam) -- not an empty database, which is the one shape this
migration's own data-migration step cannot fail in.
The migration must insert the `orders` stage directly after the last
move-spam-shaped stage, as a new appended revision, leaving the filing
rule and classify stages untouched and the pipeline_revisions history
append-only.

Every assertion here reads the appended revision the way the application
actually reads it -- through PipelineRevisionRepository, which calls
.get() on the stored document unconditionally -- rather than through a
helper that re-parses a str defensively "just in case the driver hands
one back". A migration that writes the document as a jsonb *string*
instead of a jsonb *object* (0038_orders' own released bug: a JSONB-typed
column's bind processor serializes whatever it is handed, so handing it
an already-serialized string serializes it a second time) is exactly what
such a defensive re-parse silently repairs in the test while leaving the
database, and every real reader of it, broken.
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
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.revisions import PipelineRevisionRepository
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_PREVIOUS = "0033_message_action_submissions"

_PREVIOUS_DOCUMENT: dict[str, Any] = {
    "enabled": True,
    "stages": [
        {
            "stage_id": "classify", "type": "classify", "name": "Classify spam",
            "config": {}, "enabled": True, "halt": False,
        },
        {
            "stage_id": "rule-newsletters", "type": "match", "name": "Archive newsletters",
            "config": {
                "when": {"subject_contains": "newsletter"},
                "effects": [{"move": {"special_use": "archive"}}],
            },
            "enabled": True, "halt": True,
        },
        {
            "stage_id": "move-spam", "type": "match", "name": "Move spam to junk",
            "config": {
                "when": {"verdict_is": "spam"},
                "effects": [{"move": {"special_use": "junk"}}, {"set_flags": {"seen": True}}],
            },
            "enabled": True, "halt": False,
        },
    ],
}


async def _upgrade(url: str, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(url), revision)


async def _connect(url: str) -> DatabaseConnection:
    """A DatabaseConnection against `url`, independent of the global
    singleton init_database()/close_database() manage -- this module
    upgrades its own throwaway database outside the migrated_db fixture,
    so it owns its connection's lifecycle too."""
    db = DatabaseConnection(
        DatabaseConfig(url=url, pool_size=2, max_overflow=0, reserved_for_requests=0)
    )
    await db.init()
    return db


@pytest_asyncio.fixture()
async def db_at_previous(postgres_url: str) -> AsyncIterator[str]:
    """A throwaway database migrated to the revision before this one,
    holding a real multi-stage pipeline revision -- classify, a filing
    rule, move-spam -- the shape a deployment that predates orders
    actually has, not an empty pipeline_revisions table."""
    name = f"ordersmig_{uuid.uuid4().hex[:12]}"
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
        # CAST explicitly -- the same pattern PipelineRevisionRepository
        # .append() uses -- so this baseline is unambiguously a jsonb
        # object, the shape every previous release's own writes produce.
        await conn.execute(
            text(
                "INSERT INTO pipeline_revisions (document, note) "
                "VALUES (CAST(:document AS jsonb), 'baseline')"
            ),
            {"document": json.dumps(_PREVIOUS_DOCUMENT)},
        )
    await engine.dispose()
    try:
        yield url
    finally:
        admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_orders_stage_lands_after_move_spam_in_a_real_multi_stage_pipeline(
    db_at_previous: str,
) -> None:
    await _upgrade(db_at_previous, "head")

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT revision, note, jsonb_typeof(document) AS doc_type "
                        "FROM pipeline_revisions ORDER BY revision"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    # append-only: 0006_pipeline's own default revision (empty rules,
    # spam settings' defaults) is whatever this database already carried
    # from running the migrations up to _PREVIOUS; our seeded baseline is
    # the one right after it; and exactly one more revision -- the
    # migration's own insert -- comes after that. Every one of them is a
    # jsonb *object* -- the incident this migration caused in production
    # was exactly a revision stored as a jsonb *string* instead.
    assert len(rows) == 3
    assert [row.doc_type for row in rows] == ["object", "object", "object"]
    assert rows[2].note == "Add the orders stage"

    db = await _connect(db_at_previous)
    try:
        current = await PipelineRevisionRepository(db).current()
    finally:
        await db.close()

    assert current is not None
    assert current.enabled is True
    stage_types = [(s.stage_id, s.type) for s in current.stages]
    assert stage_types == [
        ("classify", "classify"),
        ("rule-newsletters", "match"),
        ("move-spam", "match"),
        ("orders", "orders"),
    ]
    # the filing rule and move-spam stages themselves are byte-for-byte
    # untouched, not merely present under the same stage_id
    assert current.stages[1].config == _PREVIOUS_DOCUMENT["stages"][1]["config"]
    assert current.stages[1].enabled == _PREVIOUS_DOCUMENT["stages"][1]["enabled"]
    assert current.stages[1].halt == _PREVIOUS_DOCUMENT["stages"][1]["halt"]
    assert current.stages[2].config == _PREVIOUS_DOCUMENT["stages"][2]["config"]
    assert current.stages[2].enabled == _PREVIOUS_DOCUMENT["stages"][2]["enabled"]
    assert current.stages[2].halt == _PREVIOUS_DOCUMENT["stages"][2]["halt"]


@pytest.mark.asyncio
async def test_orders_stage_is_not_inserted_twice_on_a_second_upgrade_attempt(
    db_at_previous: str,
) -> None:
    """Idempotent in spirit even though alembic itself never re-runs a
    revision against a database already at head -- insert_orders_stage's
    own guard (an existing 'orders' stage short-circuits) is what this
    proves end to end, by calling the migration's insert helper against
    the already-migrated table's current revision a second time."""
    await _upgrade(db_at_previous, "head")

    db = await _connect(db_at_previous)
    try:
        from mail_verdict.pipeline.revisions import insert_orders_stage

        current = await PipelineRevisionRepository(db).current()
        assert current is not None
        stages = [
            {
                "stage_id": s.stage_id, "type": s.type, "name": s.name,
                "config": dict(s.config), "enabled": s.enabled, "halt": s.halt,
            }
            for s in current.stages
        ]
        assert insert_orders_stage(stages) == stages
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_heal_repairs_a_current_revision_already_double_encoded(
    db_at_previous: str,
) -> None:
    """Simulates a database that already ran the originally released,
    buggy 0038_orders: its appended revision stored as a jsonb *string*
    holding the document's own encoded text, rather than a jsonb
    *object* -- reproduced here at the storage level with
    to_jsonb(document::text) rather than by re-running the fixed
    migration code, since the bug this guards against is in the stored
    data, not in how it gets there. The healing revision (chained right
    after 0038_orders) must repair it, with the stages themselves
    untouched and in the same order -- production's own database is
    exactly this shape."""
    await _upgrade(db_at_previous, "0038_orders")

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            broken_revision = (
                await conn.execute(
                    text("SELECT revision FROM pipeline_revisions ORDER BY revision DESC LIMIT 1")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "UPDATE pipeline_revisions SET document = to_jsonb(document::text) "
                    "WHERE revision = :r"
                ),
                {"r": broken_revision},
            )
        async with engine.connect() as conn:
            doc_type = (
                await conn.execute(
                    text(
                        "SELECT jsonb_typeof(document) FROM pipeline_revisions WHERE revision = :r"
                    ),
                    {"r": broken_revision},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert doc_type == "string"  # the corruption actually landed before healing

    await _upgrade(db_at_previous, "head")

    db = await _connect(db_at_previous)
    try:
        current = await PipelineRevisionRepository(db).current()
    finally:
        await db.close()

    assert current is not None
    stage_types = [(s.stage_id, s.type) for s in current.stages]
    assert stage_types == [
        ("classify", "classify"),
        ("rule-newsletters", "match"),
        ("move-spam", "match"),
        ("orders", "orders"),
    ]
    assert current.stages[1].config == _PREVIOUS_DOCUMENT["stages"][1]["config"]
    assert current.stages[2].config == _PREVIOUS_DOCUMENT["stages"][2]["config"]

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.connect() as conn:
            doc_type_after = (
                await conn.execute(
                    text(
                        "SELECT jsonb_typeof(document) FROM pipeline_revisions "
                        "WHERE revision = :r"
                    ),
                    {"r": broken_revision},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert doc_type_after == "object"


@pytest.mark.asyncio
async def test_heal_statement_is_a_noop_on_a_database_that_never_had_the_fault(
    db_at_previous: str,
) -> None:
    """Idempotent, and safe to run against a database that never
    produced a double-encoded document (a fresh install with the fixed
    0038_orders, or one already healed): scoped by
    jsonb_typeof(document) = 'string', so re-running the heal statement
    a second time touches nothing."""
    await _upgrade(db_at_previous, "head")

    engine = create_async_engine(db_at_previous)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pipeline_revisions SET document = (document #>> '{}')::jsonb "
                    "WHERE jsonb_typeof(document) = 'string'"
                )
            )
    finally:
        await engine.dispose()
    assert result.rowcount == 0
