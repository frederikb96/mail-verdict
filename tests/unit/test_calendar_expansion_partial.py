"""
_expand_all's shared budget used to discard the whole batch the instant
it ran out -- a month with one slow object came back with none of the
others either, indistinguishable from an empty month. The per-object
deadline inside _expand_all_sync keeps whatever finished before the
budget ran out.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)

_STEP_SECONDS = 0.05


@pytest.mark.asyncio
async def test_budget_exhaustion_keeps_finished_objects() -> None:
    from mail_verdict.api.calendar_events import _expand_all
    from mail_verdict.database.models import DavObject

    window_start = datetime(2026, 1, 1, tzinfo=UTC)
    window_end = datetime(2026, 2, 1, tzinfo=UTC)
    objects = [DavObject(id=uuid.uuid4(), data="x") for _ in range(5)]

    def _slow(*_args: object, **_kwargs: object) -> list[object]:
        time.sleep(_STEP_SECONDS)
        return []

    # A budget wide enough for roughly two objects, narrow enough that
    # the batch cannot possibly finish inside it -- makes the boundary
    # deterministic without depending on host speed for the count itself,
    # only for it landing strictly between 0 and len(objects).
    budget = _STEP_SECONDS * 2.5
    with (
        patch("mail_verdict.api.calendar_events._EXPANSION_BUDGET_SECONDS", budget),
        patch("mail_verdict.calendar.ical.expand_instances", side_effect=_slow),
    ):
        expanded, truncated = await _expand_all(objects, window_start, window_end)

    assert truncated is True
    assert 0 < len(expanded) < len(objects), (
        f"expected a partial batch, got {len(expanded)} of {len(objects)}"
    )


@pytest.mark.asyncio
async def test_finishing_inside_the_budget_is_not_truncated() -> None:
    from mail_verdict.api.calendar_events import _expand_all
    from mail_verdict.database.models import DavObject

    window_start = datetime(2026, 1, 1, tzinfo=UTC)
    window_end = datetime(2026, 2, 1, tzinfo=UTC)
    objects = [DavObject(id=uuid.uuid4(), data="x") for _ in range(3)]

    with patch("mail_verdict.calendar.ical.expand_instances", return_value=[]):
        expanded, truncated = await _expand_all(objects, window_start, window_end)

    assert truncated is False
    assert len(expanded) == len(objects)
