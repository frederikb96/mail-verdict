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

import pytest

from mail_verdict.outbox.stalled import _describe, _Stalled

_SYNCED = datetime.now(timezone.utc) - timedelta(days=1)


def _row(*, is_active: bool | None, state: str | None, last_full_sync: datetime | None) -> _Stalled:
    return _Stalled(
        id=uuid.uuid4(), account_id=uuid.uuid4(), kind="send", subject="Held send",
        waiting_since=datetime.now(timezone.utc) - timedelta(minutes=60, seconds=5),
        is_active=is_active, state=state, last_full_sync=last_full_sync,
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            _row(is_active=True, state="error", last_full_sync=_SYNCED),
            ("disconnected", "retried"), id="disconnected-after-working",
        ),
        pytest.param(
            _row(is_active=True, state="error", last_full_sync=None),
            ("never connected", "settings"), id="never-connected",
        ),
        pytest.param(
            _row(is_active=True, state="disabled", last_full_sync=_SYNCED),
            ("disabled",), id="disabled",
        ),
        pytest.param(
            _row(is_active=False, state="error", last_full_sync=_SYNCED),
            ("paused",), id="paused-wins-over-its-state",
        ),
    ],
)
def test_an_account_that_cannot_send_is_named(row: _Stalled, expected: tuple[str, ...]) -> None:
    title, body = _describe(row)
    assert title == "Message not sent yet"
    assert body.startswith("Held send -- still waiting after 60 min: the account"), body
    for phrase in expected:
        assert phrase in body, body


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_row(is_active=True, state="active", last_full_sync=_SYNCED), id="healthy"),
        pytest.param(_row(is_active=True, state="syncing", last_full_sync=None), id="first-sync"),
        pytest.param(_row(is_active=None, state=None, last_full_sync=None), id="account-gone"),
    ],
)
def test_an_account_that_can_send_is_not_blamed(row: _Stalled) -> None:
    assert _describe(row)[1] == "Held send -- still waiting after 60 min"
