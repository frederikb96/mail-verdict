"""
Tests for the account order endpoint: a per-user preference, the
`mail.account_order` user setting, naming only accounts the caller owns.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from mail_verdict.api.account_order import router as account_order_router
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.service import init_settings_service, reset_settings_service
from tests.helpers.auth import act_as, owner_scope, user_scope

_TARGET = "mail_verdict.api.account_order.get_db_connection"


@pytest.fixture()
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(account_order_router)
    return app


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
    reset_settings_service()


async def _add_user(db: DatabaseConnection) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, display_name, role) VALUES (:id, 'U', 'user')"),
            {"id": user_id},
        )
    return user_id


def _start(client: TestClient, migrated_db: DatabaseConnection) -> None:
    client.portal.call(init_settings_service, migrated_db)  # type: ignore[union-attr]


class TestAccountOrder:
    def test_get_with_no_setting_returns_empty_order(
        self, client: TestClient, app: FastAPI, migrated_db: DatabaseConnection,
    ) -> None:
        act_as(app, owner_scope())
        _start(client, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get("/account-order")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"order": []}

    def test_put_then_get_round_trips_the_order(
        self, client: TestClient, app: FastAPI, migrated_db: DatabaseConnection,
    ) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        act_as(app, owner_scope(ids))
        _start(client, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            put_resp = client.put("/account-order", json={"order": [str(i) for i in ids]})
            assert put_resp.status_code == 200, put_resp.text
            assert put_resp.json()["order"] == [str(i) for i in ids]

            get_resp = client.get("/account-order")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["order"] == [str(i) for i in ids]

    def test_saving_a_second_time_overwrites_rather_than_duplicating(
        self, client: TestClient, app: FastAPI, migrated_db: DatabaseConnection,
    ) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        act_as(app, owner_scope(ids))
        _start(client, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            client.put("/account-order", json={"order": [str(ids[0])]})
            client.put("/account-order", json={"order": [str(ids[1]), str(ids[0])]})
            resp = client.get("/account-order")
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == [str(ids[1]), str(ids[0])]

    def test_an_order_is_one_users_own(
        self, client: TestClient, app: FastAPI, migrated_db: DatabaseConnection,
    ) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        other = client.portal.call(_add_user, migrated_db)  # type: ignore[union-attr]
        _start(client, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            act_as(app, owner_scope([mine]))
            client.put("/account-order", json={"order": [str(mine)]})
            act_as(app, user_scope(other, [theirs]))
            before = client.get("/account-order").json()
            client.put("/account-order", json={"order": [str(theirs)]})
            act_as(app, owner_scope([mine]))
            owner_after = client.get("/account-order").json()
        assert before == {"order": []}
        assert owner_after == {"order": [str(mine)]}

    def test_accounts_that_are_not_the_callers_are_never_stored_or_returned(
        self, client: TestClient, app: FastAPI, migrated_db: DatabaseConnection,
    ) -> None:
        mine, foreign = uuid.uuid4(), uuid.uuid4()
        act_as(app, owner_scope([mine]))
        _start(client, migrated_db)
        with patch(_TARGET, return_value=migrated_db):
            put = client.put("/account-order", json={"order": [str(foreign), str(mine)]})
            get = client.get("/account-order")
        assert put.json() == {"order": [str(mine)]}
        assert get.json() == {"order": [str(mine)]}
        stored = client.portal.call(  # type: ignore[union-attr]
            _stored_order, migrated_db,
        )
        assert stored == [str(mine)]


async def _stored_order(db: DatabaseConnection) -> list[str]:
    async with db.session() as session:
        row = await session.execute(
            text(
                "SELECT data->'account_order' FROM user_settings "
                "WHERE category = 'mail' AND data ? 'account_order'"
            )
        )
        values = [r[0] for r in row.all()]
    return list(values[-1])
