"""_dav_collection_kind is what tells a dav_collection/dav_object event
apart as calendar.* or contact.* on the SSE wire -- get this wrong and
every contact change would show up in the calendar UI instead."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)


def _make_db(scalar_result: object | None) -> MagicMock:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=scalar_result)
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    return db


@pytest.mark.asyncio
async def test_returns_the_collections_kind() -> None:
    from mail_verdict.server import _dav_collection_kind

    db = _make_db("addressbook")
    kind = await _dav_collection_kind(db, str(uuid.uuid4()))
    assert kind == "addressbook"


@pytest.mark.asyncio
async def test_returns_none_for_a_row_that_no_longer_exists() -> None:
    """A hard-deleted collection (its dav_account cascaded away) has
    nothing left to look up -- degrades to None rather than raising."""
    from mail_verdict.server import _dav_collection_kind

    db = _make_db(None)
    kind = await _dav_collection_kind(db, str(uuid.uuid4()))
    assert kind is None


@pytest.mark.asyncio
async def test_returns_none_for_an_unparseable_id() -> None:
    from mail_verdict.server import _dav_collection_kind

    db = _make_db("calendar")
    kind = await _dav_collection_kind(db, "not-a-uuid")
    assert kind is None
