"""Insert-only IMAP connection fields must never reach the account UPDATE.

PostIMAP's grant on `accounts` only permits UPDATE on
(is_active, imap_password, smtp_host, smtp_port, smtp_user, smtp_password,
name) -- imap_host/imap_port/imap_user are insert-only. Both compose files
run the app as the table owner, so a stray write to those columns never
fails locally; it only surfaces as `permission denied` on a deployment
where the app and PostIMAP hold separate, narrower roles.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


def _make_account(account_id: uuid.UUID) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=account_id,
        name="original-name",
        imap_host="mail.example.com",
        imap_port=993,
        imap_user="user@example.com",
        smtp_host=None,
        smtp_port=None,
        smtp_user=None,
        is_active=True,
        state="active",
        state_error=None,
        capabilities=None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture()
def client() -> TestClient:
    from mail_verdict.api.accounts import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_update_account_never_forwards_insert_only_imap_fields(
    client: TestClient,
) -> None:
    """A request naming imap_host/imap_port/imap_user must not pass them through."""
    account_id = uuid.uuid4()
    account = _make_account(account_id)

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=account)),
            MagicMock(one=MagicMock(return_value=(account, None))),
        ]
    )
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("mail_verdict.api.accounts.get_db_connection", return_value=db),
        patch(
            "mail_verdict.api.accounts.postimap_update_account", new=AsyncMock()
        ) as mocked_update,
    ):
        resp = client.patch(
            f"/accounts/{account_id}",
            json={
                "name": "renamed",
                "imap_host": "evil.example.com",
                "imap_port": 1234,
                "imap_user": "attacker",
            },
        )

    assert resp.status_code == 200
    mocked_update.assert_awaited_once()
    _args, kwargs = mocked_update.await_args
    assert "imap_host" not in kwargs
    assert "imap_port" not in kwargs
    assert "imap_user" not in kwargs
    assert kwargs.get("name") == "renamed"


def test_account_update_request_has_no_insert_only_imap_fields() -> None:
    """The request schema itself cannot carry these fields, regardless of caller."""
    from mail_verdict.api.schemas import AccountUpdateRequest

    fields = set(AccountUpdateRequest.model_fields)
    assert "imap_host" not in fields
    assert "imap_port" not in fields
    assert "imap_user" not in fields
