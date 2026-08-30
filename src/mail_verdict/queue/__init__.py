"""
Generic Postgres work-queue engine.

Everything here is parameterised by table or by name and knows nothing
about messages, embeddings or spam -- see work_queue.py's module docstring
for why. message_embeddings and pipeline_runs are expected to share this
package unchanged.
"""

from __future__ import annotations

from mail_verdict.queue.backoff import compute_backoff
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState, CircuitStatus
from mail_verdict.queue.manager import QueueManager, QueueSummary
from mail_verdict.queue.notify import ReconciliationTimer, WorkQueueNotifier, wait_for_work
from mail_verdict.queue.supervisor import WorkerSupervisor
from mail_verdict.queue.work_queue import REQUIRED_COLUMNS, WorkQueue
from mail_verdict.queue.worker_loop import default_worker_loop

__all__ = [
    "REQUIRED_COLUMNS",
    "CircuitBreaker",
    "CircuitState",
    "CircuitStatus",
    "QueueManager",
    "QueueSummary",
    "ReconciliationTimer",
    "WorkQueue",
    "WorkQueueNotifier",
    "WorkerSupervisor",
    "compute_backoff",
    "default_worker_loop",
    "wait_for_work",
]
