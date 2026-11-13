"""
The webhook routes (admin only) read the rules of the caller: the webhook a
retry or a backfill names is looked up in the caller's own pipeline document,
and a backfill reaches only the caller's accounts.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.webhooks import router
from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.pipeline.revisions import PipelineRevisionRepository
from tests.helpers.auth import act_as, owner_scope, user_scope
from tests.helpers.pipeline_pg import insert_user
from tests.pg.test_pipeline_api import _seed_account_and_folder, _seed_message


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    act_as(app, owner_scope())
    with TestClient(app) as c:
        yield c


def test_a_backfill_uses_the_callers_document_and_accounts(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    mine, mine_folder = client.portal.call(_seed_account_and_folder, migrated_db)
    other, other_folder = client.portal.call(_seed_account_and_folder, migrated_db)
    for account, folder in ((mine, mine_folder), (other, other_folder)):
        client.portal.call(
            functools.partial(_seed_message, account_id=account, folder_id=folder), migrated_db,
        )
    name = f"hook-{uuid.uuid4().hex[:8]}"
    document = {"enabled": True, "stages": [{
        "stage_id": "send", "type": "match",
        "config": {
            "when": {"subject_contains": "viagra"},
            "effects": [{"webhook": {"name": name, "url": "http://127.0.0.1:9/never"}}],
        },
    }]}
    client.portal.call(
        functools.partial(
            PipelineRevisionRepository(migrated_db).append, OWNER_USER_ID, document, note="hook",
        ),
    )
    stranger = client.portal.call(insert_user, migrated_db)
    body = {"since": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "dry_run": True}

    with patch("mail_verdict.api.webhooks.get_db_connection", return_value=migrated_db):
        act_as(client.app, owner_scope([mine]))  # type: ignore[arg-type]
        own = client.post(f"/webhooks/{name}/backfill", json=body)
        act_as(client.app, user_scope(stranger, is_admin=True))  # type: ignore[arg-type]
        foreign = client.post(f"/webhooks/{name}/backfill", json=body)

    assert own.status_code == 200, own.text
    assert (own.json()["scanned"], own.json()["matched"]) == (
        client.portal.call(_count_mail, migrated_db, mine), 1,
    )
    assert foreign.status_code == 404


async def _count_mail(db: DatabaseConnection, account_id: uuid.UUID) -> int:
    async with db.session() as session:
        return int((await session.execute(
            text("SELECT count(*) FROM messages WHERE account_id = :a"), {"a": account_id},
        )).scalar_one())
