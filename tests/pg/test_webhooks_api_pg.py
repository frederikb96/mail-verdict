"""
The webhook routes (admin only) read the rules of the caller: the webhook a
retry or a backfill names is looked up in the caller's own pipeline document,
and a listing, a retry or a backfill reaches only the caller's accounts.
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
from mail_verdict.pipeline.contracts import Webhook
from mail_verdict.pipeline.revisions import PipelineRevisionRepository
from mail_verdict.webhooks import repository
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


async def _failed_delivery(
    db: DatabaseConnection, account_id: uuid.UUID, mail_id: uuid.UUID, name: str, url: str,
) -> uuid.UUID:
    """A delivery of `mail_id` to `url` that has given up."""
    effect = Webhook(name=name, url=url)
    async with db.session() as session:
        await repository.enqueue_delivery(
            session, effect, account_id=account_id, msg_key=f"<{mail_id}>",
            message_id=mail_id, origin="live", priority=0,
        )
        return (await session.execute(
            text(
                "UPDATE webhook_deliveries SET status = 'failed' "
                "WHERE account_id = :a AND name = :n RETURNING id"
            ),
            {"a": account_id, "n": name},
        )).scalar_one()


async def _delivery(db: DatabaseConnection, delivery_id: uuid.UUID) -> tuple[str, str]:
    async with db.session() as session:
        row = (await session.execute(
            text("SELECT status, config->>'url' AS url FROM webhook_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )).one()
    return row.status, row.url


async def _forget_deliveries(db: DatabaseConnection, name: str) -> None:
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM webhook_deliveries WHERE name = :n"), {"n": name},
        )


def test_another_admins_deliveries_are_neither_listed_nor_resent_to_the_callers_url(
    client: TestClient, migrated_db: DatabaseConnection,
) -> None:
    """Two admins whose rules use the same webhook name: one retrying "every
    failed delivery" or naming the other's delivery must not aim the other's
    mail at their own URL."""
    owner_account, owner_folder = client.portal.call(_seed_account_and_folder, migrated_db)
    other_account, other_folder = client.portal.call(_seed_account_and_folder, migrated_db)
    owner_mail, other_mail = (
        client.portal.call(
            functools.partial(_seed_message, account_id=account, folder_id=folder), migrated_db,
        )
        for account, folder in ((owner_account, owner_folder), (other_account, other_folder))
    )
    other_admin = client.portal.call(functools.partial(insert_user, role="admin"), migrated_db)
    name = f"hook-{uuid.uuid4().hex[:8]}"
    owners_url, others_url = "http://127.0.0.1:9/owner", "http://127.0.0.1:9/other"
    owners_delivery = client.portal.call(
        _failed_delivery, migrated_db, owner_account, owner_mail, name, owners_url,
    )
    others_delivery = client.portal.call(
        _failed_delivery, migrated_db, other_account, other_mail, name, others_url,
    )
    client.portal.call(
        functools.partial(
            PipelineRevisionRepository(migrated_db).append, other_admin,
            {"enabled": True, "stages": [{
                "stage_id": "send", "type": "match",
                "config": {"when": {}, "effects": [{"webhook": {"name": name, "url": others_url}}]},
            }]},
            note="hook",
        ),
    )

    try:
        with patch("mail_verdict.api.webhooks.get_db_connection", return_value=migrated_db):
            act_as(client.app, user_scope(other_admin, [other_account], is_admin=True))  # type: ignore[arg-type]
            listed = client.get("/webhooks/deliveries", params={"name": name})
            one = client.post(f"/webhooks/deliveries/{owners_delivery}/retry")
            every = client.post(f"/webhooks/{name}/retry-failed")
        states = [
            client.portal.call(_delivery, migrated_db, delivery)
            for delivery in (owners_delivery, others_delivery)
        ]
    finally:
        # A pending delivery left in the shared database would be claimed by
        # the next module's worker drain.
        client.portal.call(_forget_deliveries, migrated_db, name)

    assert listed.status_code == 200, listed.text
    assert [d["id"] for d in listed.json()] == [str(others_delivery)]
    assert one.status_code == 403
    assert every.json() == {"requeued": 1}
    assert states == [("failed", owners_url), ("pending", others_url)]
