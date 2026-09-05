"""
The calendar month view's recurrence expansion runs on its own bounded
thread pool, not the loop's default one. A pathological object that never
finishes within the request's own timeout is retried on every request
that asks for it -- each attempt permanently strands one more worker in
whatever pool served it, since a thread has no cooperative way to be
interrupted mid-walk. A dedicated pool contains that damage to calendar
expansion alone, rather than eventually starving every other
asyncio.to_thread() call sharing the loop's own default executor.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

os.environ.setdefault(
    "MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)

# Long enough that the default executor's own quick call below would
# queue behind it if the two pools were actually the same one; short
# enough not to hold up the rest of the suite while the (uncancellable)
# background threads finish on their own.
_HANG_SECONDS = 1.5


@pytest.mark.asyncio
async def test_saturating_expansion_leaves_the_default_executor_free() -> None:
    from mail_verdict.api.calendar_events import _EXPANSION_EXECUTOR, _expand_all
    from mail_verdict.database.models import DavObject

    pool_size = _EXPANSION_EXECUTOR._max_workers
    window_start = datetime(2026, 1, 1, tzinfo=UTC)
    window_end = datetime(2026, 2, 1, tzinfo=UTC)
    objects = [DavObject(id=uuid.uuid4(), data="x") for _ in range(pool_size)]

    def _hang(*_args: object, **_kwargs: object) -> list[object]:
        # The real shape a pathological object takes -- one that never
        # returns quickly -- without needing an actual one.
        time.sleep(_HANG_SECONDS)
        return []

    # The loop's real default executor is min(32, cpu_count + 4) workers,
    # which would absorb saturating only `pool_size` of them regardless of
    # whether expansion shares it -- swapping in one sized to match is
    # what makes this test discriminate, on any machine's CPU count.
    loop = asyncio.get_running_loop()
    small_default = ThreadPoolExecutor(max_workers=pool_size)
    loop.set_default_executor(small_default)
    try:
        with patch("mail_verdict.calendar.ical.expand_instances", side_effect=_hang):
            # One task per object, each occupying exactly one expansion
            # worker -- saturating the whole pool, the same shape a burst
            # of month-view requests against the same pathological object
            # takes.
            tasks = [
                asyncio.create_task(_expand_all([obj], window_start, window_end))
                for obj in objects
            ]
            await asyncio.sleep(0.1)  # let every task actually start its thread

            # An ordinary asyncio.to_thread() call -- every other use of
            # the default executor in this process -- must still complete
            # quickly. If expansion shared that pool, this would queue
            # behind the hang above instead.
            started = time.monotonic()
            await asyncio.wait_for(asyncio.to_thread(time.sleep, 0.01), timeout=_HANG_SECONDS)
            elapsed = time.monotonic() - started
            assert elapsed < 1.0, (
                f"default executor took {elapsed:.2f}s while calendar-expand was saturated -- "
                "the two pools are not actually separate"
            )

            await asyncio.gather(*tasks)
    finally:
        small_default.shutdown(wait=False)
