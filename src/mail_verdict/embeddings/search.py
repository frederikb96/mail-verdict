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

The pool of nearest messages is found one of two ways. The exact scan
ranks every eligible vector: 100% recall by construction, but it reads
every vector of the model (6 KB each, stored out of line), which on a
mailbox of tens of thousands of messages is about a second warm and
several when the table is not in cache. Cost is linear in vector count,
and config.search.exact_scan_row_ceiling names the size where it should be
measured again. The index path asks the table's HNSW index for the nearest
few hundred vectors and checks only those against the scope, which reads a
tiny fraction of the vectors; it is approximate, so it runs with iterative
scanning and pgvector's maximum ef_search, and hands over to the exact
scan whenever it cannot produce a full pool. An index scan at pgvector's
default ef_search=40 returns rows at roughly a third of the right
similarity for a query that lands far from every document (dense
near-duplicate clusters trap the greedy traversal), which is why the
settings in _pool_from_index matter and why config.search.use_hnsw_index
can switch the index path off.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Select, and_, desc, select, text
from sqlalchemy.orm import aliased

from mail_verdict.database.models import GlacierMessage, Message, MessageEmbedding
from mail_verdict.glacier.rows import glacier_as_message_select, touches_glacier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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

# How many more nearest vectors the index is asked for than the pool needs,
# since some of them belong to messages that are expunged or outside the
# search's scope and drop out afterwards.
_INDEX_OVERFETCH = 1.5


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
    use_index: bool,
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

    The pool is found by ids and scores alone; only the messages that end
    up in a tier are loaded as rows, since a message row carries its whole
    body and raw source.

    Args:
        db: Database connection
        query_vector: The already-embedded query text
        model: Only rows encoded with this model are searched -- never mix
            vector spaces from two models in one ranking
        use_index: Look for the pool through the HNSW index first (see the
            module docstring); False scans exactly
        account_id: Scope to one account, or None to search every account
        folder_ids: Restrict to these folders, or None for no restriction
        k: Size of the nearest-neighbour pool the tiers are computed over
        received_after, received_before: Inclusive bounds on received_at;
            a message with no Date header matches neither

    Returns:
        Tier 1 hits then tier 2 hits, each newest first; a message without
        a date sorts last within its tier
    """
    scope = _Scope(
        model=model, query_vector=query_vector, account_id=account_id, folder_ids=folder_ids,
        received_after=received_after, received_before=received_before, k=k,
    )
    pool: list[tuple[uuid.UUID, float]] | None = None
    if use_index:
        async with db.session() as session:
            pool = await _pool_from_index(session, scope)
    if pool is None:
        async with db.session() as session:
            pool = await _pool_exact(session, scope)

    tiers = assign_tiers([similarity for _, similarity in pool])
    tier_of = {
        message_id: tier for (message_id, _), tier in zip(pool, tiers, strict=True)
        if tier is not None
    }
    if not tier_of:
        return []
    async with db.session() as session:
        messages = await _load_messages(session, list(tier_of))
    kept = [
        SemanticSearchResult(message=message, tier=tier_of[message.id]) for message in messages
    ]
    # Dateless messages stay in their tier but sort behind every dated one.
    kept.sort(key=lambda r: r.message.received_at or _EPOCH, reverse=True)
    kept.sort(key=lambda r: (r.tier, r.message.received_at is None))
    return kept


@dataclass(frozen=True)
class _Scope:
    """What one search looks at: the query and every restriction on it."""

    model: str
    query_vector: list[float]
    account_id: uuid.UUID | None
    folder_ids: list[uuid.UUID] | None
    received_after: datetime | None
    received_before: datetime | None
    k: int


async def _pool_exact(session: AsyncSession, scope: _Scope) -> list[tuple[uuid.UUID, float]]:
    """
    The k eligible messages nearest the query as (message id, similarity),
    nearest first, from an exact scan.

    The scope is joined in before the ranking, so a narrow folder or time
    window only reads the vectors of the messages inside it.
    """
    # The pgvector HNSW index on this column is approximate; these two
    # GUCs, scoped to this transaction only (a session autobegins its
    # transaction on the first statement), force Postgres to scan the
    # table exactly instead of consulting it.
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))

    distance = MessageEmbedding.embedding.cosine_distance(scope.query_vector)
    similarity = (1 - distance).label("similarity")

    live_stmt = (
        select(Message.id, similarity)
        .join(
            MessageEmbedding,
            and_(
                MessageEmbedding.account_id == Message.account_id,
                MessageEmbedding.message_id == Message.id,
            ),
        )
        .where(
            MessageEmbedding.model == scope.model,
            MessageEmbedding.status == "done",
            Message.expunged_at.is_(None),
        )
    )
    if scope.account_id is not None:
        live_stmt = live_stmt.where(Message.account_id == scope.account_id)
    if scope.folder_ids is not None:
        live_stmt = live_stmt.where(Message.folder_id.in_(scope.folder_ids))
    if scope.received_after is not None:
        live_stmt = live_stmt.where(Message.received_at >= scope.received_after)
    if scope.received_before is not None:
        live_stmt = live_stmt.where(Message.received_at <= scope.received_before)

    # A glaciered message's embedding hint is NULL by design (design
    # section 4.7 -- see GlacierMessage's own docstring), so it can
    # never be found through the live join above; joined here
    # instead on the durable (account_id, msg_key) identity, which
    # needs no hint at all. Only added when this search's own scope
    # can reach a glacier at all -- glacier_folders empty means the
    # union below never runs, the same "pays nothing" guarantee
    # database/repository.py's own text-search union gives.
    glacier_folders = await touches_glacier(
        session, account_id=scope.account_id, folder_ids=scope.folder_ids,
    )
    stmt: Any
    if glacier_folders:
        glacier_stmt = (
            select(GlacierMessage.id, (1 - distance).label("similarity"))
            .join(
                MessageEmbedding,
                and_(
                    MessageEmbedding.account_id == GlacierMessage.account_id,
                    MessageEmbedding.msg_key == GlacierMessage.msg_key,
                ),
            )
            .where(
                MessageEmbedding.model == scope.model, MessageEmbedding.status == "done",
                GlacierMessage.visible_at.is_not(None),
                GlacierMessage.folder_id.in_(glacier_folders),
            )
        )
        if scope.received_after is not None:
            glacier_stmt = glacier_stmt.where(GlacierMessage.received_at >= scope.received_after)
        if scope.received_before is not None:
            glacier_stmt = glacier_stmt.where(GlacierMessage.received_at <= scope.received_before)
        union = live_stmt.union_all(glacier_stmt).subquery()
        stmt = select(union.c.id, union.c.similarity).order_by(desc(union.c.similarity))
    else:
        stmt = live_stmt.order_by(distance)
    stmt = stmt.limit(scope.k)
    return [(row[0], float(row[1])) for row in (await session.execute(stmt)).all()]


# Iterative scanning makes the index keep going past ef_search until it has
# produced the rows asked for even though the model filter discards some of
# what it visits, and ef_search at pgvector's maximum keeps the visited
# neighbourhood wide. With sequential and bitmap scans off the planner has
# no cheaper way to order by distance than the HNSW index; left on, it
# prefers a bitmap scan of every vector of the model followed by a sort,
# which is the exact scan again.
_INDEX_SETTINGS = (
    "enable_seqscan = off",
    "enable_bitmapscan = off",
    "hnsw.ef_search = 1000",
    "hnsw.iterative_scan = strict_order",
)


def _index_candidates_stmt(scope: _Scope) -> Select[Any]:
    """The nearest vectors of the model, unscoped, for the index to answer."""
    distance = MessageEmbedding.embedding.cosine_distance(scope.query_vector)
    return (
        select(
            MessageEmbedding.account_id, MessageEmbedding.msg_key,
            MessageEmbedding.message_id, (1 - distance).label("similarity"),
        )
        .where(MessageEmbedding.model == scope.model, MessageEmbedding.status == "done")
        .order_by(distance)
        .limit(int(scope.k * _INDEX_OVERFETCH))
    )


async def _pool_from_index(
    session: AsyncSession, scope: _Scope,
) -> list[tuple[uuid.UUID, float]] | None:
    """
    The same pool as _pool_exact, found by asking the HNSW index for the
    nearest vectors first and only then checking which of them are
    eligible -- so the scope is applied to a few hundred candidates
    instead of ranking every vector in it.

    None when that did not produce k eligible messages (a narrow scope
    leaves few of the nearest candidates standing); the exact scan then
    answers, and a scope that narrow is the case where it is cheap.
    """
    for setting in _INDEX_SETTINGS:
        await session.execute(text(f"SET LOCAL {setting}"))
    candidates = (await session.execute(_index_candidates_stmt(scope))).all()

    live_stmt = select(Message.id, Message.account_id).where(
        Message.id.in_([c.message_id for c in candidates if c.message_id is not None]),
        Message.expunged_at.is_(None),
    )
    if scope.account_id is not None:
        live_stmt = live_stmt.where(Message.account_id == scope.account_id)
    if scope.folder_ids is not None:
        live_stmt = live_stmt.where(Message.folder_id.in_(scope.folder_ids))
    if scope.received_after is not None:
        live_stmt = live_stmt.where(Message.received_at >= scope.received_after)
    if scope.received_before is not None:
        live_stmt = live_stmt.where(Message.received_at <= scope.received_before)
    live = {row.id: row.account_id for row in (await session.execute(live_stmt)).all()}

    unresolved = [c for c in candidates if live.get(c.message_id) != c.account_id]
    glacier: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    glacier_folders = await touches_glacier(
        session, account_id=scope.account_id, folder_ids=scope.folder_ids,
    )
    if glacier_folders and unresolved:
        glacier_stmt = select(
            GlacierMessage.id, GlacierMessage.account_id, GlacierMessage.msg_key,
        ).where(
            GlacierMessage.msg_key.in_([c.msg_key for c in unresolved]),
            GlacierMessage.visible_at.is_not(None),
            GlacierMessage.folder_id.in_(glacier_folders),
        )
        if scope.received_after is not None:
            glacier_stmt = glacier_stmt.where(GlacierMessage.received_at >= scope.received_after)
        if scope.received_before is not None:
            glacier_stmt = glacier_stmt.where(GlacierMessage.received_at <= scope.received_before)
        glacier = {
            (row.account_id, row.msg_key): row.id
            for row in (await session.execute(glacier_stmt)).all()
        }

    pool: list[tuple[uuid.UUID, float]] = []
    for c in candidates:  # already nearest first
        if live.get(c.message_id) == c.account_id and c.message_id is not None:
            pool.append((c.message_id, float(c.similarity)))
        elif (c.account_id, c.msg_key) in glacier:
            pool.append((glacier[(c.account_id, c.msg_key)], float(c.similarity)))
    return pool[: scope.k] if len(pool) >= scope.k else None


async def _load_messages(session: AsyncSession, ids: list[uuid.UUID]) -> list[Message]:
    """The live or glaciered message rows for these ids, in no particular order."""
    live_stmt = select(Message).where(Message.id.in_(ids))
    glacier_stmt = glacier_as_message_select().where(GlacierMessage.id.in_(ids))
    entity = aliased(Message, live_stmt.union_all(glacier_stmt).subquery())
    return list((await session.execute(select(entity))).scalars().all())
