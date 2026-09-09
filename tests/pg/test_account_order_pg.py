"""
Tests for the account order endpoint: a single, instance-wide Setting row
that decides the order accounts render in, stored the same way the unified
folder order is (see mail_verdict.api.unified).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_verdict.api.account_order import router as account_order_router
from mail_verdict.database.connection import DatabaseConnection

_TARGET = "mail_verdict.api.account_order.get_db_connection"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(account_order_router)
    with TestClient(app) as c:
        yield c


class TestAccountOrder:
    def test_get_with_no_setting_returns_empty_order(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        with patch(_TARGET, return_value=migrated_db):
            resp = client.get("/account-order")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"order": []}

    def test_put_then_get_round_trips_the_order(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        with patch(_TARGET, return_value=migrated_db):
            put_resp = client.put("/account-order", json={"order": ids})
            assert put_resp.status_code == 200, put_resp.text
            assert put_resp.json()["order"] == ids

            get_resp = client.get("/account-order")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["order"] == ids

    def test_saving_a_second_time_overwrites_rather_than_duplicating(
        self, client: TestClient, migrated_db: DatabaseConnection,
    ) -> None:
        first = [str(uuid.uuid4())]
        second = [str(uuid.uuid4()), str(uuid.uuid4())]
        with patch(_TARGET, return_value=migrated_db):
            client.put("/account-order", json={"order": first})
            client.put("/account-order", json={"order": second})
            resp = client.get("/account-order")
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == second
