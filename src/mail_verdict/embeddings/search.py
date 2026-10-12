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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, desc, select, text
from sqlalchemy.orm import aliased

from mail_verdict.database.models import GlacierMessage, Message, MessageEmbedding
from mail_verdict.glacier.rows import glacier_as_message_select, touches_glacier

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Tier cut-offs on a hit's position between the pool's background level
# (the median score: what an unrelated message scores for this query) and
# its anchor (the third-best score, so one outlier hit cannot squash
# everything else): 1.0 is the anchor, 0.0 the background. Raw cosine
# similarity is meaningless across queries -- the best hit of a real query
# lands anywhere from 0.42 to 0.79 depending on its language, length and
# the corpus -- so tiers are relative to the pool's own spread. Calibrated
# against real mailbox queries: typically 5-40 hits in each tier.
TIER1_MIN = 0.55
TIER2_MIN = 0.25

# The nearest neighbours the exact scan pulls before tiering. The median is
# only a background level when the pool is large, and near-duplicate mail
# clusters (hundreds of identical alert subjects) fill a small pool with a
# single topic; the scan costs the same either way.
_CANDIDATE_POOL_SIZE = 400


Tier = Literal[1, 2]


def assign_tiers(scores: list[float]) -> list[Tier | None]:
    """
    The tier of each score within its pool: 1 (likely), 2 (less likely) or
    None (dropped). Input order is preserved; a pool of three or fewer, or
    one with no spread, is all tier 1 -- the search always shows something.
    """
    n = len(scores)
    if n <= 3:
        return [1] * n
    ranked = sorted(scores, reverse=True)
    anchor = ranked[2]
    background = ranked[n // 2]
    span = anchor - background
    if span <= 0:
        return [1] * n

    def tier(score: float) -> Tier | None:
        position = (score - background) / span
        if position >= TIER1_MIN:
            return 1
        if position >= TIER2_MIN:
            return 2
        return None

    return [tier(score) for score in scores]


@dataclass(frozen=True)
class SemanticSearchResult:
    """One hit: the message and the tier (1 or 2) it landed in."""

    message: Message
    tier: Tier


async def semantic_search(
    db: DatabaseConnection,
    *,
    query_vector: list[float],
    model: str,
    account_id: uuid.UUID | None = None,
    folder_ids: list[uuid.UUID] | None = None,
    k: int = _CANDIDATE_POOL_SIZE,
    received_after: datetime | None = None,
    received_before: datetime | None = None,
) -> list[SemanticSearchResult]:
    """
    Find the messages whose current-model vector is closest to a query
    vector and sort them into tiers (see assign_tiers): likely hits first,
    then less likely ones, each tier newest first. Messages below the
    second tier are dropped, so a query always returns its best hits and
    never a wall of noise.

    received_after/received_before narrow the nearest-neighbour pool
    itself, applied before the k-nearest cut and so before tiering -- the
    same choice search_messages makes for fulltext search, and for the same
    reason: a date range is a real restriction on what can match at all,
    not a filter over an already-decided page.

    Args:
        db: Database connection
        query_vector: The already-embedded query text
        model: Only rows encoded with this model are searched -- never mix
            vector spaces from two models in one ranking
        account_id: Scope to one account, or None to search every account
        folder_ids: Restrict to these folders, or None for no restriction
        k: Size of the nearest-neighbour pool the tiers are computed over
        received_after, received_before: Inclusive bounds on received_at;
            a message with no Date header matches neither

    Returns:
        Tier 1 hits then tier 2 hits, each newest first; a message without
        a date sorts last within its tier
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

        live_stmt = (
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
        )
        if account_id is not None:
            live_stmt = live_stmt.where(Message.account_id == account_id)
        if folder_ids is not None:
            live_stmt = live_stmt.where(Message.folder_id.in_(folder_ids))
        if received_after is not None:
            live_stmt = live_stmt.where(Message.received_at >= received_after)
        if received_before is not None:
            live_stmt = live_stmt.where(Message.received_at <= received_before)

        # A glaciered message's embedding hint is NULL by design (design
        # section 4.7 -- see GlacierMessage's own docstring), so it can
        # never be found through the live join above; joined here
        # instead on the durable (account_id, msg_key) identity, which
        # needs no hint at all. Only added when this search's own scope
        # can reach a glacier at all -- glacier_folders empty means the
        # union below never runs, the same "pays nothing" guarantee
        # database/repository.py's own text-search union gives.
        glacier_folders = await touches_glacier(
            session, account_id=account_id, folder_ids=folder_ids,
        )
        stmt: Any
        order_col: Any
        if glacier_folders:
            glacier_distance = MessageEmbedding.embedding.cosine_distance(query_vector)
            glacier_similarity = (1 - glacier_distance).label("similarity")
            glacier_stmt = (
                glacier_as_message_select()
                .add_columns(glacier_similarity)
                .join(
                    MessageEmbedding,
                    and_(
                        MessageEmbedding.account_id == GlacierMessage.account_id,
                        MessageEmbedding.msg_key == GlacierMessage.msg_key,
                    ),
                )
                .where(
                    MessageEmbedding.model == model, MessageEmbedding.status == "done",
                    GlacierMessage.folder_id.in_(glacier_folders),
                )
            )
            if received_after is not None:
                glacier_stmt = glacier_stmt.where(GlacierMessage.received_at >= received_after)
            if received_before is not None:
                glacier_stmt = glacier_stmt.where(GlacierMessage.received_at <= received_before)
            sub = live_stmt.union_all(glacier_stmt).subquery()
            entity = aliased(Message, sub)
            stmt = select(entity, sub.c.similarity)
            order_col = desc(sub.c.similarity)
        else:
            stmt = live_stmt
            order_col = distance

        stmt = stmt.order_by(order_col).limit(k)

        pool: list[tuple[Message, float]] = [
            (row[0], float(row[1])) for row in (await session.execute(stmt)).all()
        ]

    tiers = assign_tiers([similarity for _, similarity in pool])
    kept = [
        SemanticSearchResult(message=message, tier=tier)
        for (message, _), tier in zip(pool, tiers, strict=True)
        if tier is not None
    ]
    # Dateless messages stay in their tier but sort behind every dated one.
    kept.sort(key=lambda r: r.message.received_at or _EPOCH, reverse=True)
    kept.sort(key=lambda r: (r.tier, r.message.received_at is None))
    return kept
