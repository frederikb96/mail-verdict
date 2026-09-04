"""
Semantic search: nearest-neighbour lookup over message_embeddings.

Deliberately separate from the neighbour-hint machinery a future
classifier stage would use -- this is the user-facing feature the
embedding layer exists to justify, not a prompt input, so it applies none
of a hint pool's restrictions (no label-source filter, no exclusion of
sent/drafts). A user searching their own mail wants their own sent copies
found too.

The embedding row is joined to messages by its message_id join hint. That
hint can be briefly stale right after a UIDVALIDITY resync, before the
backfill reconciler notices and repoints it (embeddings/repository.py) --
narrow and self-healing, not corrected here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_, select

from mail_verdict.database.models import Message, MessageEmbedding

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection


@dataclass(frozen=True)
class SemanticSearchResult:
    """One nearest-neighbour hit: the message and how close it was."""

    message: Message
    similarity: float


async def semantic_search(
    db: DatabaseConnection,
    *,
    query_vector: list[float],
    model: str,
    account_id: uuid.UUID | None = None,
    folder_ids: list[uuid.UUID] | None = None,
    k: int = 20,
    min_similarity: float | None = None,
) -> list[SemanticSearchResult]:
    """
    Find the messages whose current-model vector is closest to a query vector.

    Args:
        db: Database connection
        query_vector: The already-embedded query text
        model: Only rows encoded with this model are searched -- never mix
            vector spaces from two models in one ranking
        account_id: Scope to one account, or None to search every account
        folder_ids: Restrict to these folders, or None for no restriction
        k: Maximum results
        min_similarity: Drop results below this cosine similarity (0..1),
            or None for no floor

    Returns:
        Results ordered nearest first
    """
    async with db.session() as session:
        distance = MessageEmbedding.embedding.cosine_distance(query_vector)
        similarity = (1 - distance).label("similarity")

        stmt = (
            select(Message, similarity)
            .join(
                MessageEmbedding,
                and_(
                    MessageEmbedding.account_id == Message.account_id,
                    MessageEmbedding.message_id == Message.id,
                ),
            )
            .where(
                MessageEmbedding.model == model,
                MessageEmbedding.status == "done",
                Message.expunged_at.is_(None),
            )
            .order_by(distance)
            .limit(k)
        )
        if account_id is not None:
            stmt = stmt.where(Message.account_id == account_id)
        if folder_ids is not None:
            stmt = stmt.where(Message.folder_id.in_(folder_ids))
        if min_similarity is not None:
            stmt = stmt.where(similarity >= min_similarity)

        rows = (await session.execute(stmt)).all()
        return [SemanticSearchResult(message=row[0], similarity=row[1]) for row in rows]
