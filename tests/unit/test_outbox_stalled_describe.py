"""
A stuck message's alert says so when its account is the reason -- PostIMAP
holds an outbox row pending while the account has no connection, so "still
waiting" alone hides the usual cause. The wording follows the consumer
contract's reading of account health: `error` is retried without end, and
last_full_sync separates an account that has worked before from one that
never connected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mail_verdict.outbox.stalled import _describe, _Stalled

# How long ago an account that has worked before last finished a sync.
_SYNCED_AGO = timedelta(days=1)


def _row(*, is_active: bool | None, state: str | None, synced: bool) -> _Stalled:
    """A send held for 60 min as of now -- built when the test runs, never at
    collection, so the age it reads is the age it was given."""
    now = datetime.now(timezone.utc)
    return _Stalled(
        id=uuid.uuid4(), account_id=uuid.uuid4(), kind="send", subject="Held send",
        waiting_since=now - timedelta(minutes=60, seconds=5),
        is_active=is_active, state=state, last_full_sync=now - _SYNCED_AGO if synced else None,
    )


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        pytest.param(
            {"is_active": True, "state": "error", "synced": True},
            ("disconnected", "retried"), id="disconnected-after-working",
        ),
        pytest.param(
            {"is_active": True, "state": "error", "synced": False},
            ("never connected", "settings"), id="never-connected",
        ),
        pytest.param(
            {"is_active": True, "state": "disabled", "synced": True},
            ("disabled",), id="disabled",
        ),
        pytest.param(
            {"is_active": False, "state": "error", "synced": True},
            ("paused",), id="paused-wins-over-its-state",
        ),
    ],
)
def test_an_account_that_cannot_send_is_named(
    account: dict[str, Any], expected: tuple[str, ...],
) -> None:
    title, body = _describe(_row(**account))
    assert title == "Message not sent yet"
    assert body.startswith("Held send -- still waiting after 60 min: the account"), body
    for phrase in expected:
        assert phrase in body, body


@pytest.mark.parametrize(
    "account",
    [
        pytest.param({"is_active": True, "state": "active", "synced": True}, id="healthy"),
        pytest.param({"is_active": True, "state": "syncing", "synced": False}, id="first-sync"),
        pytest.param({"is_active": None, "state": None, "synced": False}, id="account-gone"),
    ],
)
def test_an_account_that_can_send_is_not_blamed(account: dict[str, Any]) -> None:
    assert _describe(_row(**account))[1] == "Held send -- still waiting after 60 min"
