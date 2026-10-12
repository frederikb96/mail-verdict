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

The pool of nearest messages is found by counting the messages inside the
search's scope (account, folders, time window) first and then choosing how to
rank them:

- A scope of at most config.search.exact_scan_max_rows messages is ranked
  exactly: the scope's messages are joined to their vectors first, so only
  those vectors are read. Complete by construction, and cheap because the
  scope is small.
- A wider scope asks the HNSW index for the nearest vectors, with the time
  window and account applied inside the index scan (the embedding row carries
  a copy of the message's received_at, which never changes), and then checks
  the facts that can change -- expunged, folder, glacier visibility -- on
  those candidates. If that leaves fewer than a full pool, the index is asked
  once more for four times as many. Exactly ranking a wide scope reads every
  vector in it, which is what made a search take seconds.

The index is approximate. It runs with iterative scanning, so a filtered scan
keeps going until it has the rows asked for, and with the planner settings in
_INDEX_SETTINGS: without them the planner prefers to read every vector of the
model and sort, because the vectors live out of line and its cost estimate
does not count them. tests/pg proves the candidate query names the HNSW index.

The pool only needs to be a good sample of the nearest messages: tiers are
drawn from its median and its third-best score (assign_tiers).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Select, and_, desc, func, select, text

from mail_verdict.database.models import GlacierMessage, Message, MessageEmbedding
from mail_verdict.database.projection import MessageDisplay, load_display
from mail_verdict.glacier.rows import touches_glacier

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

# When the facts checked after the index scan (folder, expunged) leave fewer
# candidates than a pool, the index is asked once more for this many times
# the pool.
_REFILL_FACTOR = 4

# hnsw.ef_search cannot be set above this; it also bounds how many rows one
# index scan returns.
_MAX_EF_SEARCH = 1000


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

    message: MessageDisplay
    tier: Tier


@dataclass(frozen=True)
class _Scope:
    """What one search looks at: the query and every restriction on it."""

    model: str
    query_vector: list[float]
    account_id: uuid.UUID | None
    folder_ids: list[uuid.UUID] | None
    received_after: datetime | None
    received_before: datetime | None


@dataclass(frozen=True)
class _Candidate:
    """A vector the index returned: whose it is and how near."""

    account_id: uuid.UUID
    msg_key: str
    message_id: uuid.UUID | None
    similarity: float


Pool = list[tuple[uuid.UUID, float]]


async def candidate_pool(
    *,
    count: Callable[[], Awaitable[int]],
    exact: Callable[[], Awaitable[Pool]],
    index: Callable[[int], Awaitable[list[_Candidate]]],
    eligible: Callable[[list[_Candidate]], Awaitable[Pool]],
    k: int,
    exact_scan_max_rows: int,
) -> Pool:
    """
    The pool of the k nearest eligible messages as (message id,
    similarity), nearest first -- the pool rule of the module docstring,
    over its four database steps.

    Args:
        count: Messages inside the scope, counted up to just above
            exact_scan_max_rows
        exact: Ranks exactly the scope's messages
        index: The nearest n vectors from the HNSW index, scope applied
            as far as the embedding row can answer it
        eligible: Candidates that pass the facts that can change, with
            their message ids
        k: Pool size
        exact_scan_max_rows: Scope size up to which the exact scan is used
    """
    inside = await count()
    if inside == 0:
        return []
    if inside <= exact_scan_max_rows:
        return await exact()
    pool = await eligible(await index(k))
    if len(pool) < k:
        pool = await eligible(await index(k * _REFILL_FACTOR))
    # The index may hand rows back slightly out of distance order.
    pool.sort(key=lambda item: item[1], reverse=True)
    return pool[:k]


async def semantic_search(
    db: DatabaseConnection,
    *,
    query_vector: list[float],
    model: str,
    exact_scan_max_rows: int,
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
    itself, before the k-nearest cut and so before tiering -- the same
    choice search_messages makes for fulltext search, and for the same
    reason: a date range is a real restriction on what can match at all,
    not a filter over an already-decided page.

    The pool is found by ids and scores alone; only the messages that end
    up in a tier are loaded, and only the columns a result row shows.

    Args:
        db: Database connection
        query_vector: The already-embedded query text
        model: Only rows encoded with this model are searched -- never mix
            vector spaces from two models in one ranking
        exact_scan_max_rows: See the module docstring
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
        received_after=received_after, received_before=received_before,
    )

    async def count() -> int:
        async with db.session() as session:
            return await _count_in_scope(session, scope, exact_scan_max_rows)

    async def exact() -> Pool:
        async with db.session() as session:
            return await _pool_exact(session, scope, k)

    async def index(n: int) -> list[_Candidate]:
        async with db.session() as session:
            return await _pool_index(session, scope, n)

    async def eligible(candidates: list[_Candidate]) -> Pool:
        async with db.session() as session:
            return await _keep_eligible(session, scope, candidates)

    pool = await candidate_pool(
        count=count, exact=exact, index=index, eligible=eligible,
        k=k, exact_scan_max_rows=exact_scan_max_rows,
    )

    tiers = assign_tiers([similarity for _, similarity in pool])
    tier_of = {
        message_id: tier for (message_id, _), tier in zip(pool, tiers, strict=True)
        if tier is not None
    }
    if not tier_of:
        return []
    async with db.session() as session:
        messages = await load_display(session, list(tier_of))
    kept = [
        SemanticSearchResult(message=message, tier=tier_of[message_id])
        for message_id, message in messages.items()
    ]
    # Dateless messages stay in their tier but sort behind every dated one.
    kept.sort(key=lambda r: r.message.received_at or _EPOCH, reverse=True)
    kept.sort(key=lambda r: (r.tier, r.message.received_at is None))
    return kept


def _message_scope(scope: _Scope, table: Any, stmt: Any) -> Any:
    """Narrow a select over messages or glacier_messages to the search scope.
    The glacier's folder restriction is separate: it names glacier folders."""
    if scope.received_after is not None:
        stmt = stmt.where(table.received_at >= scope.received_after)
    if scope.received_before is not None:
        stmt = stmt.where(table.received_at <= scope.received_before)
    return stmt


def _live_scope(scope: _Scope, stmt: Any) -> Any:
    stmt = stmt.where(Message.expunged_at.is_(None))
    if scope.account_id is not None:
        stmt = stmt.where(Message.account_id == scope.account_id)
    if scope.folder_ids is not None:
        stmt = stmt.where(Message.folder_id.in_(scope.folder_ids))
    return _message_scope(scope, Message, stmt)


def _glacier_scope(scope: _Scope, stmt: Any, glacier_folders: Any) -> Any:
    stmt = stmt.where(
        GlacierMessage.visible_at.is_not(None), GlacierMessage.folder_id.in_(glacier_folders),
    )
    return _message_scope(scope, GlacierMessage, stmt)


async def _count_in_scope(session: AsyncSession, scope: _Scope, limit: int) -> int:
    """How many messages (live and glaciered) the scope holds, counted only
    up to `limit` + 1 so an unrestricted search does not walk the mailbox."""
    glacier_folders = await touches_glacier(
        session, account_id=scope.account_id, folder_ids=scope.folder_ids,
    )
    live = _live_scope(scope, select(Message.id)).limit(limit + 1)
    total = (await session.execute(select(func.count()).select_from(live.subquery()))).scalar_one()
    if glacier_folders and total <= limit:
        glacier = _glacier_scope(scope, select(GlacierMessage.id), glacier_folders).limit(
            limit + 1,
        )
        total += (
            await session.execute(select(func.count()).select_from(glacier.subquery()))
        ).scalar_one()
    return int(total)


async def _pool_exact(session: AsyncSession, scope: _Scope, k: int) -> Pool:
    """
    The k eligible messages nearest the query as (message id, similarity),
    nearest first, from an exact scan.

    The scope is joined in before the ranking, so only the vectors of the
    messages inside it are read.
    """
    # The pgvector HNSW index on this column is approximate; these two
    # GUCs, scoped to this transaction only (a session autobegins its
    # transaction on the first statement), force Postgres to scan the
    # table exactly instead of consulting it.
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))

    distance = MessageEmbedding.embedding.cosine_distance(scope.query_vector)
    similarity = (1 - distance).label("similarity")

    live_stmt = _live_scope(
        scope,
        select(Message.id, similarity)
        .join(
            MessageEmbedding,
            and_(
                MessageEmbedding.account_id == Message.account_id,
                MessageEmbedding.message_id == Message.id,
            ),
        )
        .where(MessageEmbedding.model == scope.model, MessageEmbedding.status == "done"),
    )

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
        glacier_stmt = _glacier_scope(
            scope,
            select(GlacierMessage.id, (1 - distance).label("similarity"))
            .join(
                MessageEmbedding,
                and_(
                    MessageEmbedding.account_id == GlacierMessage.account_id,
                    MessageEmbedding.msg_key == GlacierMessage.msg_key,
                ),
            )
            .where(MessageEmbedding.model == scope.model, MessageEmbedding.status == "done"),
            glacier_folders,
        )
        union = live_stmt.union_all(glacier_stmt).subquery()
        stmt = select(union.c.id, union.c.similarity).order_by(desc(union.c.similarity))
    else:
        stmt = live_stmt.order_by(distance)
    return [(row[0], float(row[1])) for row in (await session.execute(stmt.limit(k))).all()]


# The planner settings of the index query. The vectors are stored out of
# line and the planner's cost estimate does not count them, so without these
# it prefers reading every vector of the model (a bitmap scan on the model,
# or the btree on the window) and sorting, which is the exact scan again.
# With sequential and bitmap scans and sorting off, the only way left to
# satisfy ORDER BY distance is the HNSW index. jit is off because the index
# scan's cost estimate is high enough to trigger compilation, which costs
# more than the query. Relaxed-order iterative scanning keeps the scan going
# until it has the rows asked for even though the model, window and account
# filters discard some of what it visits; the pool is sorted afterwards.
_INDEX_SETTINGS = (
    "enable_seqscan = off",
    "enable_bitmapscan = off",
    "enable_sort = off",
    "jit = off",
    "hnsw.iterative_scan = relaxed_order",
)


def _index_candidates_stmt(scope: _Scope, n: int) -> Select[Any]:
    """The n nearest vectors of the model within what the embedding row
    itself can answer: account and time window."""
    distance = MessageEmbedding.embedding.cosine_distance(scope.query_vector)
    stmt = (
        select(
            MessageEmbedding.account_id, MessageEmbedding.msg_key,
            MessageEmbedding.message_id, (1 - distance).label("similarity"),
        )
        .where(MessageEmbedding.model == scope.model, MessageEmbedding.status == "done")
        .order_by(distance)
        .limit(n)
    )
    if scope.account_id is not None:
        stmt = stmt.where(MessageEmbedding.account_id == scope.account_id)
    if scope.received_after is not None:
        stmt = stmt.where(MessageEmbedding.received_at >= scope.received_after)
    if scope.received_before is not None:
        stmt = stmt.where(MessageEmbedding.received_at <= scope.received_before)
    return stmt


async def _apply_index_settings(session: AsyncSession, n: int) -> None:
    """Set, for the current transaction, what the index query needs to be
    answered by the HNSW index when it asks for n rows."""
    for setting in _INDEX_SETTINGS:
        await session.execute(text(f"SET LOCAL {setting}"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {min(n, _MAX_EF_SEARCH)}"))


async def _pool_index(session: AsyncSession, scope: _Scope, n: int) -> list[_Candidate]:
    """The n nearest vectors from the HNSW index, scope applied as far as
    the embedding row can answer it."""
    await _apply_index_settings(session, n)
    return [
        _Candidate(row.account_id, row.msg_key, row.message_id, float(row.similarity))
        for row in (await session.execute(_index_candidates_stmt(scope, n))).all()
    ]


async def _keep_eligible(
    session: AsyncSession, scope: _Scope, candidates: list[_Candidate],
) -> Pool:
    """
    The candidates whose message passes everything the embedding row cannot
    vouch for -- not expunged, in the folders asked for, visible if
    glaciered, and (again, from the message itself) inside the window --
    as (message id, similarity). A live message is found through the join
    hint, a glaciered one through msg_key.
    """
    live_stmt = _live_scope(
        scope,
        select(Message.id, Message.account_id).where(
            Message.id.in_([c.message_id for c in candidates if c.message_id is not None]),
        ),
    )
    live = {row.id: row.account_id for row in (await session.execute(live_stmt)).all()}

    unresolved = [c for c in candidates if live.get(c.message_id) != c.account_id]
    glacier: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    glacier_folders = await touches_glacier(
        session, account_id=scope.account_id, folder_ids=scope.folder_ids,
    )
    if glacier_folders and unresolved:
        glacier_stmt = _glacier_scope(
            scope,
            select(GlacierMessage.id, GlacierMessage.account_id, GlacierMessage.msg_key).where(
                GlacierMessage.msg_key.in_([c.msg_key for c in unresolved]),
            ),
            glacier_folders,
        )
        glacier = {
            (row.account_id, row.msg_key): row.id
            for row in (await session.execute(glacier_stmt)).all()
        }

    pool: Pool = []
    for c in candidates:
        if c.message_id is not None and live.get(c.message_id) == c.account_id:
            pool.append((c.message_id, c.similarity))
        elif (c.account_id, c.msg_key) in glacier:
            pool.append((glacier[(c.account_id, c.msg_key)], c.similarity))
    return pool
