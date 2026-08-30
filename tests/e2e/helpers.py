"""
Polling helpers for the e2e layer.

Syncs are asynchronous (an account takes roughly ten seconds to reach
`active`; a non-INBOX folder's first backfill can lag behind that). A
fixed sleep-then-check is how a suite like this goes flaky and then gets
ignored -- every wait here polls a real condition with a bounded timeout
and fails with a message naming exactly what never happened.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from starlette.testclient import TestClient

T = TypeVar("T")

TEST_DOMAIN = "e2e.test.local"


def unique_email(prefix: str) -> str:
    """A unique mailbox address for one test -- Dovecot creates it on first login."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}@{TEST_DOMAIN}"


def wait_for(
    check: Callable[[], T | None],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    description: str,
) -> T:
    """Poll `check` until it returns a truthy value, or raise naming `description`.

    A `check` returning None/False/empty counts as "not yet"; anything
    else is the awaited result, returned to the caller.
    """
    deadline = time.monotonic() + timeout_s
    last_result: T | None = None
    while time.monotonic() < deadline:
        last_result = check()
        if last_result:
            return last_result
        time.sleep(interval_s)
    raise TimeoutError(
        f"{description} did not happen within {timeout_s}s (last observed: {last_result!r})"
    )


async def wait_for_async(
    check: Callable[[], Awaitable[T | None]],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    description: str,
) -> T:
    """The `await`-aware twin of wait_for, for a check that itself needs a DB session.

    `wait_for` calling an `async def check` would just collect never-awaited
    coroutine objects (always truthy) and return on the first iteration
    without checking anything -- this is the version that actually awaits.
    """
    deadline = time.monotonic() + timeout_s
    last_result: T | None = None
    while time.monotonic() < deadline:
        last_result = await check()
        if last_result:
            return last_result
        await asyncio.sleep(interval_s)
    raise TimeoutError(
        f"{description} did not happen within {timeout_s}s (last observed: {last_result!r})"
    )


def wait_for_account_active(
    client: TestClient, account_id: str, timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll an account until PostIMAP reports it `active`.

    Fails immediately (not after the full timeout) if PostIMAP reports
    `error` instead -- that state will never self-resolve into `active`.
    """
    deadline = time.monotonic() + timeout_s
    last_state = "unknown"
    while time.monotonic() < deadline:
        resp = client.get(f"/api/accounts/{account_id}")
        assert resp.status_code == 200, resp.text
        account = resp.json()
        last_state = account["state"]
        if last_state == "active":
            return account
        if last_state == "error":
            raise AssertionError(f"Account entered error state: {account['state_error']}")
        time.sleep(1)
    raise TimeoutError(
        f"Account {account_id} did not reach 'active' within {timeout_s}s "
        f"(last state: {last_state!r})"
    )


def wait_for_folder(
    client: TestClient, account_id: str, imap_name: str, timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll an account's folder list until one with the given imap_name appears."""
    def _check() -> dict[str, Any] | None:
        resp = client.get(f"/api/accounts/{account_id}/folders")
        assert resp.status_code == 200, resp.text
        for folder in resp.json():
            if folder["imap_name"] == imap_name:
                return folder
        return None

    return wait_for(_check, timeout_s=timeout_s, description=f"Folder {imap_name!r} discovered")


def wait_for_mailpit_message(
    base_url: str, subject: str, timeout_s: float = 20.0,
) -> dict[str, Any]:
    """Poll Mailpit's HTTP API until a message with the given subject shows up.

    Proves PostIMAP actually transmitted the outbox row over SMTP -- a
    real delivery to a real (if throwaway) SMTP sink, not a mock.
    """
    def _check() -> dict[str, Any] | None:
        resp = httpx.get(f"{base_url}/api/v1/messages", timeout=5.0)
        assert resp.status_code == 200, resp.text
        for summary in resp.json()["messages"]:
            if summary["Subject"] == subject:
                return summary
        return None

    return wait_for(_check, timeout_s=timeout_s, description=f"Mailpit received {subject!r}")
