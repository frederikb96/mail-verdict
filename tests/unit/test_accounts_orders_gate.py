"""
Switching orders on for an account needs both orders tasks to have a model:
the gate names the task that is missing, so the settings page can say which.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_verdict.api.accounts import router
from mail_verdict.settings.defaults import SETTING_DEFAULTS, SettingCategory


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _enable(client: TestClient, **orders: Any) -> Any:
    service = MagicMock()
    service.get.return_value = {**SETTING_DEFAULTS[SettingCategory.ORDERS], **orders}
    with (
        patch("mail_verdict.api.accounts.get_settings_service", return_value=service),
        patch("mail_verdict.api.accounts.get_db_connection", return_value=MagicMock()),
    ):
        return client.patch(f"/accounts/{uuid.uuid4()}", json={"orders_enabled": True})


@pytest.mark.parametrize(
    ("models", "missing"),
    [
        ({}, "decide and write"),
        ({"decide_model": "m"}, "write"),
        ({"write_model": "m"}, "decide"),
    ],
)
def test_orders_cannot_be_enabled_until_both_tasks_have_a_model(
    client: TestClient, models: dict[str, str], missing: str,
) -> None:
    resp = _enable(client, **models)

    assert resp.status_code == 409
    assert resp.json()["detail"] == f"Set the orders {missing} model in Settings first"
