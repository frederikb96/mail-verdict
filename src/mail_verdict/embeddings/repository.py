"""
Repository for message_embeddings: the backfill enqueue, writing a
finished vector back, and coverage status.

The backfill is a self-advancing set-difference predicate run in batches,
not a cursor walk -- there is no cursor to persist, it is resumable by
construction, and "how many remain" is a count over the same predicate
rather than a separately maintained number that can drift from it. See
enqueue_missing_batch for the one subtlety a cursor-free predicate runs
into: msg_key's content-hash fallback for headerless mail cannot be
expressed in SQL without duplicating database/msg_key.py's algorithm
there, so headered mail (the overwhelming majority) is filtered in SQL and
headerless mail is filtered in Python against the same batch.
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

    async def write_result(
        self,
        item_id: uuid.UUID,
        *,
        worker_id: str,
        embedding: list[float],
        model: str,
        content_level: str,
        source_hash: str,
    ) -> bool:
        """
        Write a finished vector and move the row to 'done', guarded the
        same way queue/work_queue.py's own terminal transitions are.

        Args:
            item_id: Row to complete
            worker_id: Must match the row's current claimant
            embedding: The computed vector
            model: Model that produced it (recorded again defensively --
                matches the row's own model column by construction, but a
                guard predicate is cheap insurance against a stale claim)
            content_level: 'full' or 'envelope'
            source_hash: Hash of the exact text embedded

        Returns:
            True if this worker still held the claim and the write landed
        """
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
            )
            return (result.rowcount or 0) > 0  # type: ignore[attr-defined]

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
