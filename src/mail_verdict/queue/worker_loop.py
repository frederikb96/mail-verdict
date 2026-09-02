"""
The canonical claim -> process -> release-on-stop loop.

Not required to use WorkQueue -- every method above is public and a caller
may drive it directly -- but this is the shape every queue in the design
ends up needing, so it lives here once rather than being rewritten per
queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from mail_verdict.queue.notify import wait_for_work
from mail_verdict.queue.work_queue import WorkQueue

logger = logging.getLogger(__name__)

ItemHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


@contextlib.asynccontextmanager
async def heartbeat_while(
    work_queue: WorkQueue,
    item_id: UUID,
    *,
    worker_id: str,
    lease_seconds: float,
    interval_seconds: float | None = None,
) -> AsyncIterator[None]:
    """
    Extend `item_id`'s lease on a fixed interval for as long as the
    context body runs.

    Without this, a call that legitimately takes longer than the lease
    (a slow provider, not a dead worker) gets reclaimed and re-run by
    another worker while the first call is still in flight -- on a paid
    model call that is a duplicated charge, and every reclaim also
    increments the row's attempt count, so a slow spell can drive a row to
    permanent failure while its original call was still going to succeed.

    Args:
        work_queue: Queue the row was claimed from
        item_id: Row currently being processed
        worker_id: Must match the row's current claimant
        lease_seconds: Lease duration each heartbeat extends by
        interval_seconds: Gap between heartbeats; None defaults to a third
            of `lease_seconds`, so one missed beat still leaves margin
            before the lease actually expires -- a floor here would
            invert that relationship for a short lease, so there isn't one
    """
    gap = interval_seconds if interval_seconds is not None else lease_seconds / 3

    async def _beat() -> None:
        # An unhandled exception here kills this task silently -- nothing
        # awaits it until the context manager's own finally block, which
        # only runs once the body below has already finished. Left
        # unguarded, one database blip stops every future beat too: the
        # lease then expires mid-item, the reconciliation timer reclaims
        # it without refunding the attempt this call already spent, and a
        # second worker re-runs it -- a duplicated paid provider call for
        # the embedding and pipeline workers this wraps. Catching and
        # logging keeps the loop itself alive to retry next interval,
        # which is what the lease-margin the docstring above describes
        # actually depends on.
        while True:
            await asyncio.sleep(gap)
            try:
                await work_queue.heartbeat(
                    [item_id], worker_id=worker_id, lease_seconds=lease_seconds,
                )
            except Exception:
                logger.exception(
                    "Heartbeat failed to extend lease; retrying next interval",
                    extra={"item_id": str(item_id), "worker_id": worker_id},
                )

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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
    means. While a row is being handled, its lease is kept alive by
    `heartbeat_while` -- a call that runs long is extended rather than
    reclaimed out from under the worker still processing it.

    `heartbeat_while` only ever extends the lease of the one row it wraps.
    With `batch_size` greater than one, every other row this claimed under
    the same lease sits unrenewed while `handle_item` works through the
    rest of the batch in order -- a row late in the batch can have its
    lease expire, be reclaimed by the reconciliation timer without its
    attempt refunded, and be re-claimed and re-run by this same worker
    once it loops back around, all while the first run is (or was) still
    in flight. Safe only when either `batch_size` is 1, or processing one
    item is fast enough relative to `lease_seconds / batch_size` that this
    can never happen -- see embeddings/worker.py's own history for what it
    costs when that margin runs out.

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
            async with heartbeat_while(
                work_queue, row["id"], worker_id=worker_id, lease_seconds=lease_seconds,
            ):
                await handle_item(row)
