"""
Repository for message_embeddings: the live-arrival enqueue, the backfill
enqueue, writing a finished vector back (or a permanent failure), and
coverage status.

The backfill is a self-advancing set-difference predicate run in batches,
not a cursor walk -- there is no cursor to persist, it is resumable by
construction, and "how many remain" is a count over the same predicate
rather than a separately maintained number that can drift from it. See
enqueue_missing_batch for the one subtlety a cursor-free predicate runs
into: msg_key's content-hash fallback for headerless mail cannot be
expressed in SQL without duplicating database/msg_key.py's algorithm
there, so headered mail (the overwhelming majority) is filtered in SQL and
headerless mail is filtered in Python against the same batch.

write_result and fail() are also where the embedding-first gate lives: a
message becomes eligible for the pipeline queue only once its embedding
reaches a terminal state, done or failed. Both call
pipeline.enqueue.enqueue_pipeline_run_if_live_eligible inside the same
transaction that moves this row to its terminal status, which is what
makes the second enqueue exactly-once with the first -- see that
function's docstring for what "live-eligible" means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import Folder, Message, MessageEmbedding
from mail_verdict.database.msg_key import compute_msg_key

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.service import SettingsService

# In-scope messages this batch inspected before Python-side filtering, and
# how many genuinely new rows were inserted -- the caller stops looping
# once the first number drops below the batch size, which is what makes
# the loop terminate even when a handful of candidates keep reappearing
# (see enqueue_missing_batch's docstring).
EnqueueBatchResult = tuple[int, int]


@dataclass(frozen=True)
class EmbeddingStatus:
    """Coverage snapshot for one model, optionally scoped to one account."""

    model: str
    in_scope: int
    encoded: int
    pending: int
    failed: int

    @property
    def coverage(self) -> float:
        """Fraction of in-scope messages already encoded, 1.0 if none are in scope."""
        if self.in_scope == 0:
            return 1.0
        return self.encoded / self.in_scope


class EmbeddingRepository:
    """CRUD and coverage queries over message_embeddings."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Args:
            db: Database connection
        """
        self._db = db

    async def enqueue_missing_batch(
        self, *, model: str, batch_size: int, account_id: uuid.UUID | None = None,
    ) -> EnqueueBatchResult:
        """
        Enqueue one batch of messages missing a current-model embedding.

        The SQL anti-join filters on `message_embeddings.message_id`,
        which is exact and cheap for the ordinary case: a message keeps
        the same id for its whole life. After a UIDVALIDITY resync a
        message's id changes but a header it already has does not, so
        this call also re-checks each SQL candidate's real msg_key in
        Python (computed the same way database/msg_key.py computes it for
        every other table) against message_embeddings by
        (account_id, msg_key, model) -- the durable identity -- before
        deciding to insert. A hit under a different message_id updates
        that row's join hint instead of inserting a duplicate, which is
        what stops the resynced row from being reselected as a candidate
        on the next call once its hint points at the new id.

        Args:
            model: Embedding model this batch is enqueuing for
            batch_size: Maximum SQL candidates to consider this call
            account_id: Scope to one account, or None for every account

        Returns:
            (candidates seen, rows actually inserted) -- the caller loops
            until candidates seen is less than batch_size, not until
            inserted is zero, so a message not currently embeddable (see
            below) cannot make the loop spin forever
        """
        async with self._db.session() as session:
            not_embedded = ~select(MessageEmbedding.id).where(
                MessageEmbedding.account_id == Message.account_id,
                MessageEmbedding.message_id == Message.id,
                MessageEmbedding.model == model,
            ).exists()
            stmt = (
                select(
                    Message.id, Message.account_id, Message.message_id.label("message_id_hdr"),
                    Message.from_addr, Message.subject, Message.received_at, Message.size_bytes,
                )
                .join(Folder, Folder.id == Message.folder_id)
                .where(
                    Message.expunged_at.is_(None),
                    Folder.deleted_at.is_(None),
                    Folder.initial_sync_done.is_(True),
                    not_embedded,
                )
                .order_by(Message.received_at.desc().nulls_last(), Message.id)
                .limit(batch_size)
            )
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)

            candidates = (await session.execute(stmt)).all()
            if not candidates:
                return (0, 0)

            keys_by_candidate = {
                row.id: compute_msg_key(
                    account_id=row.account_id, message_id_hdr=row.message_id_hdr,
                    from_addr=row.from_addr, subject=row.subject,
                    received_at=row.received_at, size_bytes=row.size_bytes,
                )
                for row in candidates
            }

            existing = await session.execute(
                select(
                    MessageEmbedding.id, MessageEmbedding.account_id,
                    MessageEmbedding.msg_key, MessageEmbedding.message_id,
                ).where(
                    MessageEmbedding.model == model,
                    tuple_(MessageEmbedding.account_id, MessageEmbedding.msg_key).in_(
                        [(row.account_id, keys_by_candidate[row.id]) for row in candidates]
                    ),
                )
            )
            existing_by_key = {(row.account_id, row.msg_key): row for row in existing.all()}

            to_insert: list[dict[str, Any]] = []
            for row in candidates:
                key = keys_by_candidate[row.id]
                found = existing_by_key.get((row.account_id, key))
                if found is None:
                    to_insert.append({
                        "account_id": row.account_id, "msg_key": key,
                        "message_id": row.id, "model": model,
                    })
                elif found.message_id != row.id:
                    await session.execute(
                        update(MessageEmbedding)
                        .where(MessageEmbedding.id == found.id)
                        .values(message_id=row.id)
                    )

            inserted = 0
            if to_insert:
                stmt_ins = pg_insert(MessageEmbedding).values(to_insert)
                stmt_ins = stmt_ins.on_conflict_do_nothing(
                    index_elements=["account_id", "msg_key", "model"],
                )
                result = await session.execute(stmt_ins)
                inserted = result.rowcount or 0  # type: ignore[attr-defined]

            return (len(candidates), inserted)

    async def enqueue_one(
        self, *, account_id: uuid.UUID, message_id: uuid.UUID, model: str, priority: int = 0,
    ) -> bool:
        """
        Enqueue a single message's embedding immediately, ahead of the
        batched backfill sweep (priority 0 against its 100) -- the
        live-arrival path this piece of the design's build plan requires:
        a message is embedded before it can ever reach the pipeline queue,
        rather than waiting for the periodic backfill reconciler to notice
        it (see pipeline/enqueue.py's enqueue_live_arrival).

        Mirrors enqueue_missing_batch's resync-repoint behaviour for one
        row: if this (account_id, msg_key, model) already exists under a
        different message_id (a UIDVALIDITY resync), it is repointed
        rather than duplicated, and nothing new is inserted.

        Args:
            account_id: Account the message belongs to
            message_id: The message's current messages.id
            model: Embedding model to enqueue for
            priority: Claim ordering -- lower claims first, same
                convention pipeline_runs uses for live vs. sweep work

        Returns:
            True if a new pending row was inserted
        """
        async with self._db.session() as session:
            row = (await session.execute(
                select(
                    Message.account_id, Message.message_id.label("message_id_hdr"),
                    Message.from_addr, Message.subject, Message.received_at, Message.size_bytes,
                    Message.expunged_at, Message.is_draft,
                ).where(Message.id == message_id, Message.account_id == account_id)
            )).one_or_none()
            if row is None or row.expunged_at is not None or row.is_draft:
                return False

            key = compute_msg_key(
                account_id=account_id, message_id_hdr=row.message_id_hdr,
                from_addr=row.from_addr, subject=row.subject,
                received_at=row.received_at, size_bytes=row.size_bytes,
            )
            existing = (await session.execute(
                select(MessageEmbedding.id, MessageEmbedding.message_id).where(
                    MessageEmbedding.account_id == account_id,
                    MessageEmbedding.msg_key == key,
                    MessageEmbedding.model == model,
                )
            )).one_or_none()
            if existing is not None:
                if existing.message_id != message_id:
                    await session.execute(
                        update(MessageEmbedding)
                        .where(MessageEmbedding.id == existing.id)
                        .values(message_id=message_id)
                    )
                return False

            stmt_ins = pg_insert(MessageEmbedding).values(
                account_id=account_id, msg_key=key, message_id=message_id,
                model=model, priority=priority,
            )
            stmt_ins = stmt_ins.on_conflict_do_nothing(
                index_elements=["account_id", "msg_key", "model"],
            )
            result = await session.execute(stmt_ins)
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def write_result(
        self,
        item_id: uuid.UUID,
        *,
        worker_id: str,
        embedding: list[float],
        model: str,
        content_level: str,
        source_hash: str,
        settings_service: SettingsService,
    ) -> bool:
        """
        Write a finished vector, move the row to 'done', and -- in the
        same transaction -- gate the message's pipeline run on this
        terminal state (see the module docstring).

        Args:
            item_id: Row to complete
            worker_id: Must match the row's current claimant
            embedding: The computed vector
            model: Model that produced it (recorded again defensively --
                matches the row's own model column by construction, but a
                guard predicate is cheap insurance against a stale claim)
            content_level: 'full' or 'envelope'
            source_hash: Hash of the exact text embedded
            settings_service: Read for the live-eligibility check's
                pipeline.live_max_age_days guard

        Returns:
            True if this worker still held the claim and the write landed
        """
        from mail_verdict.pipeline.enqueue import enqueue_pipeline_run_if_live_eligible
        from mail_verdict.queue.notify import WorkQueueNotifier

        async with self._db.session() as session:
            result = await session.execute(
                update(MessageEmbedding)
                .where(
                    MessageEmbedding.id == item_id,
                    MessageEmbedding.status == "claimed",
                    MessageEmbedding.claimed_by == worker_id,
                    MessageEmbedding.model == model,
                )
                .values(
                    status="done", embedding=embedding, content_level=content_level,
                    source_hash=source_hash, claimed_by=None, claimed_at=None,
                    lease_expires_at=None, updated_at=datetime.now(timezone.utc),
                )
                .returning(MessageEmbedding.account_id, MessageEmbedding.message_id)
            )
            row = result.one_or_none()
            if row is None:
                return False
            enqueued = await enqueue_pipeline_run_if_live_eligible(
                session, account_id=row.account_id, message_id=row.message_id,
                settings_service=settings_service,
            )
            if enqueued:
                await WorkQueueNotifier.notify(session, "pipeline")
            return True

    async def fail(
        self,
        item_id: uuid.UUID,
        *,
        worker_id: str,
        last_error: str,
        settings_service: SettingsService,
    ) -> bool:
        """
        Move a claimed row permanently to 'failed' and, in the same
        transaction, gate the message's pipeline run on this terminal
        state (see the module docstring).

        A message whose embedding can never succeed must still be
        classified rather than stranded forever waiting on a vector that
        will never exist -- reaching 'failed' is as terminal as reaching
        'done', so it opens the same gate. The classify stage sees no
        neighbour hints for it, and records that in its own trace.

        Args:
            item_id: Row to fail
            worker_id: Must match the row's current claimant
            last_error: Recorded on the row for the failure list
            settings_service: Read for the live-eligibility check's
                pipeline.live_max_age_days guard

        Returns:
            True if this worker still held the claim and the update landed
        """
        from mail_verdict.pipeline.enqueue import enqueue_pipeline_run_if_live_eligible
        from mail_verdict.queue.notify import WorkQueueNotifier

        async with self._db.session() as session:
            result = await session.execute(
                update(MessageEmbedding)
                .where(
                    MessageEmbedding.id == item_id,
                    MessageEmbedding.status == "claimed",
                    MessageEmbedding.claimed_by == worker_id,
                )
                .values(
                    status="failed", last_error=last_error, claimed_by=None, claimed_at=None,
                    lease_expires_at=None, updated_at=datetime.now(timezone.utc),
                )
                .returning(MessageEmbedding.account_id, MessageEmbedding.message_id)
            )
            row = result.one_or_none()
            if row is None:
                return False
            enqueued = await enqueue_pipeline_run_if_live_eligible(
                session, account_id=row.account_id, message_id=row.message_id,
                settings_service=settings_service,
            )
            if enqueued:
                await WorkQueueNotifier.notify(session, "pipeline")
            return True

    async def status(
        self, *, model: str, account_id: uuid.UUID | None = None,
    ) -> EmbeddingStatus:
        """
        Coverage snapshot for one model.

        Args:
            model: Embedding model to report on
            account_id: Scope to one account, or None for every account

        Returns:
            in_scope/encoded/pending/failed counts and their coverage ratio
        """
        async with self._db.session() as session:
            scope_stmt = (
                select(func.count())
                .select_from(Message)
                .join(Folder, Folder.id == Message.folder_id)
                .where(
                    Message.expunged_at.is_(None),
                    Folder.deleted_at.is_(None),
                    Folder.initial_sync_done.is_(True),
                )
            )
            if account_id is not None:
                scope_stmt = scope_stmt.where(Message.account_id == account_id)
            in_scope = (await session.execute(scope_stmt)).scalar_one()

            counts_stmt = (
                select(
                    func.count().filter(MessageEmbedding.status == "done").label("encoded"),
                    func.count()
                    .filter(MessageEmbedding.status.in_(("pending", "claimed")))
                    .label("pending"),
                    func.count().filter(MessageEmbedding.status == "failed").label("failed"),
                )
                .select_from(MessageEmbedding)
                .where(MessageEmbedding.model == model)
            )
            if account_id is not None:
                counts_stmt = counts_stmt.where(MessageEmbedding.account_id == account_id)
            counts = (await session.execute(counts_stmt)).one()

            return EmbeddingStatus(
                model=model, in_scope=in_scope,
                encoded=counts.encoded, pending=counts.pending, failed=counts.failed,
            )
