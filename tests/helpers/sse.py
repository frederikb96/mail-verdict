"""Driving the SSE generator and endpoint without a database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import mail_verdict.api.events as events_module
from mail_verdict.api.events import KeyResolver
from mail_verdict.auth.principal import Principal, bind_principal
from mail_verdict.auth.scope import Scope


def fixed_keys(*keys: str) -> KeyResolver:
    """A key resolver for _sse_generator that always answers `keys`."""

    async def resolve() -> frozenset[str]:
        return frozenset(keys)

    return resolve


@contextmanager
def streaming_as(monkeypatch: pytest.MonkeyPatch, scope: Scope) -> Iterator[None]:
    """Make sse_endpoint's caller `scope`: a principal bound for the block,
    and the scope it resolves to answered without a database."""
    assert scope.user_id is not None

    async def scope_of(principal: Principal) -> Scope:
        return scope

    monkeypatch.setattr(events_module, "_scope_of", scope_of)
    with bind_principal(Principal(user_id=scope.user_id, subject="sse", is_admin=scope.is_admin)):
        yield
