"""
The canonical claim -> process -> release-on-stop loop.

Not required to use WorkQueue -- every method above is public and a caller
may drive it directly -- but this is the shape every queue in the design
ends up needing, so it lives here once rather than being rewritten per
queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mail_verdict.queue.notify import wait_for_work
from mail_verdict.queue.work_queue import WorkQueue

ItemHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


async def default_worker_loop(
    work_queue: WorkQueue,
    *,
    worker_id: str,
    stop_event: asyncio.Event,
    batch_size: int,
    lease_seconds: float,
    handle_item: ItemHandler,
    wake_event: asyncio.Event | None = None,
    poll_interval: float = 1.0,
) -> None:
    """
    Claim a batch, hand each row to `handle_item`, repeat until stopped.

    `handle_item` is expected to leave the row in a terminal state itself
    (via `complete`, `fail`, `retry` or `release_untouched` on the same
    `work_queue`) -- this loop only decides *when* to claim and when to
    give up on the rest of an in-progress batch, never what a row's outcome
    means.

    On a stop request, any row already claimed in the current batch but not
    yet handed to `handle_item` is released immediately with its attempt
    refunded, rather than left claimed until its lease expires -- this is
    what makes a rolling restart resume work right away instead of waiting
    out a lease.

    Args:
        work_queue: Queue to claim from and report outcomes to
        worker_id: This worker's identifier
        stop_event: Checked before claiming and before each item in a batch
        batch_size: Rows to claim per iteration
        lease_seconds: Lease duration for each claim
        handle_item: Coroutine given one claimed row; must leave it terminal
        wake_event: If given, waited on (with `poll_interval` fallback) when
            a claim comes back empty, instead of a plain sleep
        poll_interval: Fallback wait when there is nothing to claim
    """
    while not stop_event.is_set():
        claimed = await work_queue.claim_batch(
            worker_id=worker_id, batch_size=batch_size, lease_seconds=lease_seconds,
        )
        if not claimed:
            if wake_event is not None:
                await wait_for_work(wake_event, poll_interval=poll_interval)
            else:
                await asyncio.sleep(poll_interval)
            continue

        for row in claimed:
            if stop_event.is_set():
                await work_queue.release_untouched(row["id"], worker_id=worker_id)
                continue
            await handle_item(row)
