"""
Semantic search: exact nearest-neighbour lookup over message_embeddings.

Deliberately separate from the neighbour-hint machinery a future
classifier stage would use -- this is the user-facing feature the
embedding layer exists to justify, not a prompt input, so it applies none
of a hint pool's restrictions (no label-source filter, no exclusion of
sent/drafts). A user searching their own mail wants their own sent copies
found too.

The embedding row is joined to messages by its message_id join hint.
embeddings/repository.py's enqueue_missing_batch/enqueue_one only ever
repoint that hint when it is dead (NULL, or resolving to no live,
non-expunged message), so a healthy hint here always resolves -- an
account with a stale hint shows up in EmbeddingStatusResponse's
unreachable/shadowed counts, not as a silent gap in these results.

Retrieval scans the embedding table exactly rather than through the
approximate HNSW index it also carries: measured against production
(14,088 vectors), an HNSW scan at pgvector's default ef_search=40 -- what
this index actually runs at -- returns rows at roughly a third of the
right similarity for a query that lands far from every document, the
mailbox's own dense near-duplicate clusters (hundreds of identical
alert/newsletter subjects) trapping the greedy graph traversal before it
ever reaches the real neighbourhood. Raising ef_search to 400 fixes it on
that one corpus, but that number is a property of how duplicate-heavy the
mailbox is, which only grows -- and its failure mode is silent: it looks
exactly like "that mail does not exist". The exact scan has no such
constant to get wrong: 100% recall by construction, measured at 254ms on
the same corpus (fully cached; a second identical query measured 123ms).
Cost is linear in vector count, roughly 18 microseconds per vector at
1536 dimensions -- config.search.exact_scan_row_ceiling names the size
where this should be revisited.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import and_, select, text

from mail_verdict.database.models import Message, MessageEmbedding

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

Strictness = Literal["loose", "balanced", "strict"]

# Relative-to-best-match factor per strictness position: an absolute
# similarity floor cannot work here --
# observed top similarity for a real query moves between 0.41 and 0.59
# depending on query length and language, so a fixed cutoff returns
# everything for one query and nothing for another. Relative-to-the-best-
# match adapts to that automatically: a narrow topic comes back tight, a
# genuinely broad one comes back broad.
_STRICTNESS_FACTORS: dict[Strictness, float] = {"loose": 0.60, "balanced": 0.75, "strict": 0.88}

# Absolute floor beneath the relative cutoff, regardless of strictness --
# justified by the observed noise level of a query that lands far from
# every document (an HNSW-trapped result scored 0.17-0.18 on the same
# corpus this was measured against).
_ABSOLUTE_SIMILARITY_FLOOR = 0.25

# How many nearest neighbours the exact scan pulls before the strictness
# cutoff narrows them -- generous enough that "loose" and a broad query
# together aren't starved of rows to keep.
_CANDIDATE_POOL_SIZE = 200


@dataclass(frozen=True)
class SemanticSearchResult:
    """One nearest-neighbour hit: the message and how close it was."""

    message: Message
    similarity: float


@dataclass(frozen=True)
class SemanticSearchOutcome:
    """A semantic search's results plus what the relative strictness
    cutoff resolved to for this particular query -- min_similarity_applied
    is what the UI reports back to explain a small result set, since the
    number itself means nothing outside the query it was computed for."""

    results: list[SemanticSearchResult]
    min_similarity_applied: float


async def semantic_search(
    db: DatabaseConnection,
    *,
    query_vector: list[float],
    model: str,
    account_id: uuid.UUID | None = None,
    folder_ids: list[uuid.UUID] | None = None,
    k: int = _CANDIDATE_POOL_SIZE,
    strictness: Strictness = "balanced",
) -> SemanticSearchOutcome:
    """
    Find the messages whose current-model vector is closest to a query
    vector, cut down by a strictness level relative to the best match in
    this pool rather than an absolute floor (see the module docstring).

    Args:
        db: Database connection
        query_vector: The already-embedded query text
        model: Only rows encoded with this model are searched -- never mix
            vector spaces from two models in one ranking
        account_id: Scope to one account, or None to search every account
        folder_ids: Restrict to these folders, or None for no restriction
        k: Size of the nearest-neighbour pool the strictness cutoff is
            computed over
        strictness: How much of the pool the cutoff keeps -- "loose",
            "balanced" (default) or "strict"

    Returns:
        Results ordered nearest first, and the absolute similarity the
        relative cutoff resolved to for this query
    """
    async with db.session() as session:
        # The pgvector HNSW index on this column returns at most
        # ef_search rows (pgvector default 40) and, worse, can return the
        # wrong neighbourhood entirely for a query far from every
        # document -- see the module docstring. These two GUCs, scoped to
        # this transaction only, are what forces Postgres to scan the
        # table exactly instead of consulting that index; dropping the
        # index itself is the clean end state but is a migration. A
        # session autobegins its transaction on the first statement, so
        # these two land in the same transaction as the query below --
        # SET LOCAL's scope -- with no separate begin() needed.
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))

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

        pool = [
            SemanticSearchResult(message=row[0], similarity=row[1])
            for row in (await session.execute(stmt)).all()
        ]

    if not pool:
        return SemanticSearchOutcome(results=[], min_similarity_applied=_ABSOLUTE_SIMILARITY_FLOOR)

    best = pool[0].similarity  # already ordered nearest-first by the query above
    factor = _STRICTNESS_FACTORS[strictness]
    min_similarity_applied = max(best * factor, _ABSOLUTE_SIMILARITY_FLOOR)
    results = [r for r in pool if r.similarity >= min_similarity_applied]
    return SemanticSearchOutcome(results=results, min_similarity_applied=min_similarity_applied)
