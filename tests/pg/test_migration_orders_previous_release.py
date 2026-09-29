"""
0034_orders against the state the previous release reaches: a real,
multi-stage pipeline revision already in place (classify, a filing rule,
move-spam) -- not an empty database, which is the one shape this
migration's own data-migration step cannot fail in (see the "Every
migration test runs against an empty database" note in .claude/CLAUDE.md).
The migration must insert the `orders` stage directly after the last
move-spam-shaped stage, as a new appended revision, leaving the filing
rule and classify stages untouched and the pipeline_revisions history
append-only.
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
from tests.pg.test_migration_from_v1 import _POSTIMAP_STUBS, _alembic_config

_PREVIOUS = "0033_message_action_submissions"


def _doc(value: object) -> dict[str, Any]:
    """pipeline_revisions.document is JSONB; a bare text() query returns
    it already parsed as a dict, but normalise defensively in case the
    driver ever hands back the raw JSON string instead."""
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed

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
        await conn.execute(
            text("INSERT INTO pipeline_revisions (document, note) VALUES (:document, 'baseline')"),
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
                        "SELECT revision, document, note FROM pipeline_revisions "
                        "ORDER BY revision"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    # append-only: 0006_pipeline's own default revision (empty rules,
    # spam settings' defaults) is whatever this database already carried
    # from running the migrations up to _PREVIOUS; our seeded baseline is
    # the one right after it; and exactly one more revision -- the
    # migration's own insert -- comes after that.
    assert len(rows) == 3
    baseline, appended = rows[1], rows[2]
    assert _doc(baseline.document) == _PREVIOUS_DOCUMENT

    stages = _doc(appended.document)["stages"]
    stage_types = [(s["stage_id"], s["type"]) for s in stages]
    assert stage_types == [
        ("classify", "classify"),
        ("rule-newsletters", "match"),
        ("move-spam", "match"),
        ("orders", "orders"),
    ]
    # the filing rule and move-spam stages themselves are byte-for-byte
    # untouched, not merely present under the same stage_id
    assert stages[1] == _PREVIOUS_DOCUMENT["stages"][1]
    assert stages[2] == _PREVIOUS_DOCUMENT["stages"][2]
    assert _doc(appended.document)["enabled"] is True
    assert appended.note == "Add the orders stage"


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

    engine = create_async_engine(db_at_previous)
    try:
        from mail_verdict.pipeline.revisions import insert_orders_stage

        async with engine.connect() as conn:
            current = (
                await conn.execute(
                    text(
                        "SELECT document FROM pipeline_revisions ORDER BY revision DESC LIMIT 1"
                    )
                )
            ).scalar_one()
        stages = _doc(current)["stages"]
        assert insert_orders_stage(stages) == stages
    finally:
        await engine.dispose()
