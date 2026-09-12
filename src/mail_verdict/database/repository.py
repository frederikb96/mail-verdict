"""
Repository layer for database operations.

All queries are account_id scoped for multi-account isolation.
PostIMAP owns message ingestion — MailVerdict reads/queries messages
and manages its own tables (verdicts, tags, prefs, settings).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from sqlalchemy import (
    Text,
    and_,
    case,
    cast,
    delete,
    desc,
    func,
    not_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mail_verdict.core.cursor import after_cursor, after_tier_cursor
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Alert,
    Attachment,
    Folder,
    FolderPrefs,
    MailTag,
    Message,
    PushSubscription,
    SyncNotification,
    TagSource,
    Verdict,
    VerdictSource,
)
from mail_verdict.database.msg_key import compute_msg_key
from mail_verdict.push.channels import channel_for_kind

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

# Candidate imap_name values for a role, tried by case-insensitive exact
# match when no folder advertises the role's special_use flag (and no
# folder_prefs.special_use_override names it either). Archive is the role
# this matters for in practice -- RFC 6154's \Archive is optional and many
# real servers never send it, unlike \Trash and \Junk which are close to
# universal.
_ROLE_NAME_FALLBACKS: dict[str, tuple[str, ...]] = {
    "archive": ("archive", "archives"),
    "trash": ("trash", "deleted items", "deleted messages"),
    "junk": ("junk", "spam", "junk e-mail", "bulk mail"),
    "inbox": ("inbox",),
}


class AccountRepository:
    """Repository for Account queries."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_by_id(self, account_id: uuid.UUID) -> Account | None:
        """
        Get an account by ID.

        Args:
            account_id: Account UUID

        Returns:
            Account if found, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            return result.scalar_one_or_none()

    async def get_all(self) -> list[Account]:
        """
        Get all accounts.

        Returns:
            List of all Account objects
        """
        async with self._db.session() as session:
            result = await session.execute(select(Account))
            return list(result.scalars().all())


class AccountPrefsRepository:
    """Repository for AccountPrefs CRUD operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_or_create(self, account_id: uuid.UUID) -> AccountPrefs:
        """
        Get existing prefs or create defaults for an account.

        Args:
            account_id: Account UUID

        Returns:
            AccountPrefs for the account
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(AccountPrefs).where(
                    AccountPrefs.account_id == account_id,
                )
            )
            prefs = result.scalar_one_or_none()
            if prefs is not None:
                return prefs

            prefs = AccountPrefs(account_id=account_id)
            session.add(prefs)
            await session.flush()
            await session.refresh(prefs)
            return prefs

    async def update(
        self,
        account_id: uuid.UUID,
        **kwargs: Any,
    ) -> AccountPrefs:
        """
        Update account prefs fields.

        Creates the prefs row if it doesn't exist yet.

        Args:
            account_id: Account UUID
            **kwargs: Fields to update (emoji, spam_enabled, folder_order)

        Returns:
            Updated AccountPrefs
        """
        async with self._db.session() as session:
            # Upsert: insert defaults then update on conflict
            stmt = (
                pg_insert(AccountPrefs)
                .values(account_id=account_id, **kwargs)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_=kwargs,
                )
                .returning(AccountPrefs)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_by_account(self, account_id: uuid.UUID) -> AccountPrefs | None:
        """
        Get prefs for an account (without auto-creation).

        Args:
            account_id: Account UUID

        Returns:
            AccountPrefs if exists, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(AccountPrefs).where(
                    AccountPrefs.account_id == account_id,
                )
            )
            return result.scalar_one_or_none()


class FolderPrefsRepository:
    """Repository for FolderPrefs CRUD operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_or_create(self, folder_id: uuid.UUID) -> FolderPrefs:
        """
        Get existing prefs or create defaults for a folder.

        Args:
            folder_id: Folder UUID

        Returns:
            FolderPrefs for the folder
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(FolderPrefs).where(
                    FolderPrefs.folder_id == folder_id,
                )
            )
            prefs = result.scalar_one_or_none()
            if prefs is not None:
                return prefs

            prefs = FolderPrefs(folder_id=folder_id)
            session.add(prefs)
            await session.flush()
            await session.refresh(prefs)
            return prefs

    async def update(
        self,
        folder_id: uuid.UUID,
        **kwargs: Any,
    ) -> FolderPrefs:
        """
        Update folder prefs fields.

        Creates the prefs row if it doesn't exist yet.

        Args:
            folder_id: Folder UUID
            **kwargs: Fields to update (is_visible, display_name,
                      special_use_override)

        Returns:
            Updated FolderPrefs
        """
        async with self._db.session() as session:
            stmt = (
                pg_insert(FolderPrefs)
                .values(folder_id=folder_id, **kwargs)
                .on_conflict_do_update(
                    index_elements=["folder_id"],
                    set_=kwargs,
                )
                .returning(FolderPrefs)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_by_folder(self, folder_id: uuid.UUID) -> FolderPrefs | None:
        """
        Get prefs for a folder (without auto-creation).

        Args:
            folder_id: Folder UUID

        Returns:
            FolderPrefs if exists, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(FolderPrefs).where(
                    FolderPrefs.folder_id == folder_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_by_account(self, account_id: uuid.UUID) -> list[FolderPrefs]:
        """
        Get all folder prefs for an account's folders.

        Args:
            account_id: Account UUID

        Returns:
            List of FolderPrefs for all folders belonging to the account
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(FolderPrefs)
                .join(Folder, FolderPrefs.folder_id == Folder.id)
                .where(Folder.account_id == account_id)
            )
            return list(result.scalars().all())


# The four scopeable parts of a search-page query. "to" matches to_addrs as
# a whole -- each element is already a "Name <addr>" string (see from_addr),
# so casting the JSONB array to text and matching against that finds a hit
# in either the name or the address of any recipient, without unnesting.
SEARCH_FIELDS = frozenset({"subject", "from", "to", "body"})

# "relevance" (field tier then date, the historical and still-default
# behaviour) or "chronological" (date alone, ignoring tier entirely) --
# see search_messages for what each does to the ORDER BY and the cursor.
SearchSort = Literal["relevance", "chronological"]


def _ilike_escape(token: str) -> str:
    """Escape ILIKE's own wildcards in raw user input before wrapping it
    in %...% -- a query containing a literal % or _ must match that
    character, not be treated as a pattern."""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _resolve_lexemes(session: AsyncSession, query: str) -> list[str]:
    """The distinct lexemes Postgres's own 'simple' text search parser
    extracts from the raw query -- the single tokenization every later
    piece of a search (the prefix tsquery, the per-field ILIKE scope, the
    match tier, the snippet) agrees on. Splitting on whitespace in Python
    instead would let matching and the tsvector index quietly disagree on
    what a "token" is; going through Postgres's own parser is also what
    makes the tsquery injection-proof -- raw user text is never
    interpolated into tsquery syntax directly, only tokenized first.
    """
    lexeme_col = func.unnest(func.to_tsvector("simple", query)).table_valued("lexeme").c.lexeme
    result = await session.execute(select(lexeme_col).distinct())
    return [row[0] for row in result.all()]


def _tsquery_text(tokens: list[str], *, prefix: bool = True) -> str | None:
    """AND tsquery syntax over already-tokenized lexemes (every token
    required, `:*` unless prefix=False asks for the lexeme itself).
    None -- not an empty string -- for no lexemes at all (e.g. a query of
    pure punctuation), so `to_tsquery('simple', NULL)`, and therefore
    `search_vector @@ NULL`, is what "no results" resolves to: ordinary
    SQL NULL propagation rather than a special case a caller has to
    remember to check for.

    A lexeme carrying a single quote (the 'simple' config does not split
    a contraction) is escaped the same way tsquery's own quoted-lexeme
    syntax requires -- doubling it, the same rule SQL string literals
    use.
    """
    if not tokens:
        return None
    suffix = ":*" if prefix else ""
    return " & ".join(f"'{token.replace(chr(39), chr(39) * 2)}'{suffix}" for token in tokens)


def _field_predicate(msg: Any, tokens: list[str], fields: frozenset[str]) -> Any:
    """The exact field-scope restriction (every token must match at least
    one of the toggled fields), checked per column against the candidate
    set produced by _build_candidate_query -- never against one
    concatenated haystack, which would let a token spanning two fields'
    boundary match wrongly."""
    conditions = []
    for token in tokens:
        pattern = f"%{_ilike_escape(token)}%"
        parts = []
        if "subject" in fields:
            parts.append(msg.subject.ilike(pattern))
        if "from" in fields:
            parts.append(msg.from_addr.ilike(pattern))
        if "to" in fields:
            parts.append(cast(msg.to_addrs, Text).ilike(pattern))
        if "body" in fields:
            parts.append(msg.body_text.ilike(pattern))
        conditions.append(or_(*parts))
    return and_(*conditions)


# How many field tiers _match_tier ranks a whole-word match across, and
# therefore what a prefix-only match is offset by. The trigram fallback
# sits one past everything the primary stage can produce.
_FIELD_TIERS = 4
FALLBACK_MATCH_TIER = _FIELD_TIERS * 2


def _match_tier(msg: Any, tokens: list[str]) -> Any:
    """How well a candidate matched, ranked on two things: whether the
    query words are words in this message at all, and then which field
    they landed in. Computed over the real columns rather than through
    ts_rank -- PostIMAP's search_vector carries no setweight labels, so
    ts_rank provably cannot tell a subject hit from a body hit (observed:
    it ranks a body-only newsletter above a subject match). A tier is
    also explainable to a person, which is the property the ranking
    actually needs.

    Recall matches a prefix, which is what makes a partial word find
    anything and what makes the search fast. Ranking must not inherit
    that: searching a short word otherwise buries it under every longer
    word beginning with it, newest first, because both land in the same
    tier. So a message whose own lexemes include every query word comes
    first, and one that merely starts a longer word with them follows --
    within each half, by field:

    0: every token in subject
    1: every token in subject or from_addr
    2: every token in subject, from_addr or to_addrs
    3: otherwise -- at least one token is a body-only match (the
       candidate set is subject|from|body plus an explicit to_addrs
       branch, so failing all three of the above can only mean body)
    4-7: the same four, for a candidate matched only as a prefix

    Whole-word matching reads search_vector, the tsvector PostIMAP
    already maintains over subject, from_addr and body_text, so it costs
    a lexeme lookup rather than parsing any text per row. An address is
    one lexeme in that vector -- `name@example.com` never yields `name`
    -- so a hit inside from_addr or to_addrs counts as whole on its own
    terms: an address is an identifier somebody is typing part of, and
    demoting it would rank a person's own mail below a stranger's.
    """

    def _hits(*cols: Any) -> Any:
        return and_(
            *[or_(*[col.ilike(f"%{_ilike_escape(t)}%") for col in cols]) for t in tokens]
        )

    subject_tier = _hits(msg.subject)
    from_tier = _hits(msg.subject, msg.from_addr)
    to_tier = _hits(msg.subject, msg.from_addr, cast(msg.to_addrs, Text))
    field_tier = case((subject_tier, 0), (from_tier, 1), (to_tier, 2), else_=3)

    exact_text = _tsquery_text(tokens, prefix=False)
    whole_word = or_(
        msg.search_vector.op("@@")(func.to_tsquery("simple", exact_text)),
        _hits(msg.from_addr, cast(msg.to_addrs, Text)),
    )
    return field_tier + case((whole_word, 0), else_=_FIELD_TIERS)


def _build_candidate_query(
    account_id: uuid.UUID | None,
    tokens: list[str],
    folder_ids: Sequence[uuid.UUID] | None,
    fields: frozenset[str],
    *,
    received_after: datetime | None = None,
    received_before: datetime | None = None,
    is_seen: bool | None = None,
) -> tuple[Any, Any] | None:
    """The primary-recall candidate set: tsquery-matched rows (subject,
    from_addr and body_text -- what search_vector covers) UNIONed with an
    explicit to_addrs branch when 'to' is requested. search_vector does
    not cover to_addrs at all, and folding the to_addrs check in as an OR
    instead of a UNION measured 44x slower (647ms vs 14.7ms) -- it
    defeats the GIN index and forces a full detoast of every row.

    The union is wrapped in a subquery and re-aliased onto Message so a
    caller can layer the per-field restriction and the tier computation
    on top of one thing, the same shape whether or not the union ran.

    received_after/received_before narrow the candidate set itself (both
    branches of the union alike) rather than being applied to the page
    returned -- a message with no Date header (received_at IS NULL) never
    matches either bound, the same choice search's NULLS LAST ordering
    already makes for a dateless message: nothing to compare a range
    against, so it is excluded rather than assumed in or out of range.

    Returns:
        (subquery, aliased Message), or None when the query has no
        lexemes at all -- nothing to build a candidate set from.
    """
    tsquery_text = _tsquery_text(tokens)
    if tsquery_text is None:
        return None
    primary = Message.search_vector.op("@@")(func.to_tsquery("simple", tsquery_text))

    base = select(Message).where(Message.expunged_at.is_(None))
    if account_id is not None:
        base = base.where(Message.account_id == account_id)
    if folder_ids is not None:
        base = base.where(Message.folder_id.in_(folder_ids))
    if received_after is not None:
        base = base.where(Message.received_at >= received_after)
    if received_before is not None:
        base = base.where(Message.received_at <= received_before)
    if is_seen is not None:
        base = base.where(Message.is_seen == is_seen)

    candidate_stmt: Any = base.where(primary)
    if "to" in fields:
        to_predicate = and_(
            *[cast(Message.to_addrs, Text).ilike(f"%{_ilike_escape(t)}%") for t in tokens]
        )
        candidate_stmt = candidate_stmt.union(base.where(to_predicate))

    sub = candidate_stmt.subquery()
    return sub, aliased(Message, sub)


def _row_haystack(m: Message, fields: frozenset[str]) -> str:
    """The same fields _field_predicate scoped to, concatenated for
    _build_snippet -- built from the already-fetched ORM row rather than
    a second SQL-computed column, since the candidate query already
    returns full Message objects."""
    parts: list[str] = []
    if "subject" in fields:
        parts.append(m.subject or "")
    if "from" in fields:
        parts.append(m.from_addr or "")
    if "to" in fields:
        parts.append(" ".join(m.to_addrs) if m.to_addrs else "")
    if "body" in fields:
        parts.append(m.body_text or "")
    return " ".join(parts)


def _fallback_token_predicate(token: str) -> Any:
    """A token matches the fallback tier literally, or -- pg_trgm's word-
    similarity operator, never the word_similarity() function call --
    close enough that a typo doesn't lose the hit. Subject and from_addr
    only: body is never trigram-matched here, that is the
    18-second-per-query cost this whole rewrite exists to stop running.

    The operator form (`column %> token`) is required for a trigram index
    to serve this at all -- verified by EXPLAIN across all three
    spellings: `column %> 'token'` and `'token' <% column` both use the
    index, `word_similarity(token, column) >= threshold` is a sequential
    scan every time despite computing the identical answer. Its threshold
    is `pg_trgm.word_similarity_threshold`, a session GUC the operator
    reads rather than an argument it takes -- see
    search_messages_fallback, which sets it to the 0.6 validated against
    real typos before this runs.

    An '@' token is left literal-only: two unrelated addresses sharing a
    domain score *higher* on word similarity than a genuine typo does, so
    there is no threshold that keeps the typo and drops the collision. An
    address is a literal identifier someone is typing exactly, not prose
    worth typo-tolerance over.
    """
    escaped = _ilike_escape(token)
    pattern = f"%{escaped}%"
    if "@" in token:
        return or_(Message.subject.ilike(pattern), Message.from_addr.ilike(pattern))
    return or_(
        Message.subject.ilike(pattern),
        Message.from_addr.ilike(pattern),
        Message.subject.op("%>")(token),
        Message.from_addr.op("%>")(token),
    )


def _build_snippet(haystack: str, tokens: list[str], window: int = 60) -> str | None:
    """A **bold**-marked excerpt centred on the first token found as a
    literal substring (case-insensitive) -- the same markers the UI's
    renderSnippet already expects. A row that matched purely on trigram
    similarity (no literal substring present anywhere) falls back to a
    plain leading excerpt, still useful context even unmarked."""
    lower = haystack.lower()
    for token in tokens:
        idx = lower.find(token.lower())
        if idx == -1:
            continue
        start = max(0, idx - window)
        end = min(len(haystack), idx + len(token) + window)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(haystack) else ""
        return (
            f"{prefix}{haystack[start:idx]}"
            f"**{haystack[idx : idx + len(token)]}**"
            f"{haystack[idx + len(token) : end]}{suffix}"
        )
    excerpt = haystack.strip()
    if not excerpt:
        return None
    return excerpt[:140] + "…" if len(excerpt) > 140 else excerpt


class MessageRepository:
    """
    Repository for Message read operations.

    All queries are scoped by account_id. PostIMAP handles message
    ingestion — this repository is read-only for messages.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_by_id(
        self,
        account_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> Message | None:
        """
        Get a single message by ID with account scoping.

        Args:
            account_id: Account scope
            message_id: Message UUID

        Returns:
            Message if found and owned by account, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Message).where(
                    Message.id == message_id,
                    Message.account_id == account_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_by_folder(
        self,
        folder_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        """
        Get messages in a folder, newest first.

        Excludes expunged messages (expunged_at IS NOT NULL).

        Args:
            folder_id: Folder to list
            limit: Max results
            offset: Skip count

        Returns:
            Messages in the folder
        """
        async with self._db.session() as session:
            stmt = (
                select(Message)
                .where(
                    Message.folder_id == folder_id,
                    Message.expunged_at.is_(None),
                )
                .order_by(desc(Message.received_at))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_by_folder_and_uid(
        self,
        folder_id: uuid.UUID,
        imap_uid: int,
    ) -> Message | None:
        """
        Get a single message by folder and IMAP UID.

        Args:
            folder_id: Folder UUID
            imap_uid: IMAP UID within folder

        Returns:
            Message if found, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Message).where(
                    Message.folder_id == folder_id,
                    Message.imap_uid == imap_uid,
                )
            )
            return result.scalar_one_or_none()

    async def get_by_message_id(
        self,
        account_id: uuid.UUID,
        message_id: str,
    ) -> list[Message]:
        """
        Find messages by RFC 2822 Message-ID header.

        Args:
            account_id: Account scope
            message_id: RFC 2822 Message-ID header value

        Returns:
            Matching messages (may span folders)
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Message).where(
                    Message.account_id == account_id,
                    Message.message_id == message_id,
                )
            )
            return list(result.scalars().all())

    async def search_fulltext_with_snippet(
        self,
        account_id: uuid.UUID | None,
        query: str,
        *,
        limit: int = 20,
    ) -> list[tuple[Message, str]]:
        """
        Full-text search returning a highlighted snippet per result.

        The snippet is built from the same coalesced subject/from/body text
        the generated search_vector column itself indexes on, so a
        truncated message with no body still gets a snippet from its
        subject/sender rather than an empty one.

        Args:
            account_id: Account scope, or None to search across all accounts
            query: Search query string
            limit: Max results

        Returns:
            (Message, snippet) pairs ranked by relevance
        """
        async with self._db.session() as session:
            ts_query = func.websearch_to_tsquery("simple", query)
            searchable_text = (
                func.coalesce(Message.subject, "")
                + " "
                + func.coalesce(Message.from_addr, "")
                + " "
                + func.coalesce(Message.body_text, "")
            )
            snippet = func.ts_headline(
                "simple", searchable_text, ts_query,
                "StartSel=**, StopSel=**, MaxWords=35, MinWords=15",
            )
            rank = func.ts_rank(Message.search_vector, ts_query)

            stmt = (
                select(Message, snippet.label("snippet"))
                .where(
                    Message.expunged_at.is_(None),
                    Message.search_vector.op("@@")(ts_query),
                )
                .order_by(desc(rank))
                .limit(limit)
            )
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)

            result = await session.execute(stmt)
            return [(row[0], row[1]) for row in result.all()]

    async def tokenize(self, query: str) -> list[str]:
        """The distinct lexemes Postgres's 'simple' parser extracts from a
        raw search query -- computed once per request and threaded through
        search_messages, search_messages_fallback, resolve_search_cursor
        and count_search_candidates so every one of them agrees on what a
        "token" is. See _resolve_lexemes for why this goes through
        Postgres rather than a Python-side split.
        """
        async with self._db.session() as session:
            return await _resolve_lexemes(session, query)

    async def search_date_bounds(
        self,
        account_id: uuid.UUID | None,
        *,
        folder_ids: Sequence[uuid.UUID] | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        """
        (oldest, newest) received_at across the given scope -- independent
        of any query text, since the date-range control's own slider needs
        an axis to draw before a word has been typed. Dateless messages
        (received_at IS NULL) never affect either bound: MIN/MAX already
        ignore NULL, the same way the range predicate itself excludes them.

        Args:
            account_id: Account scope, or None for every account
            folder_ids: Restrict to these folders, or None for no restriction

        Returns:
            (None, None) when the scope has no dated messages at all
        """
        stmt = select(
            func.min(Message.received_at), func.max(Message.received_at),
        ).where(Message.expunged_at.is_(None))
        if account_id is not None:
            stmt = stmt.where(Message.account_id == account_id)
        if folder_ids is not None:
            stmt = stmt.where(Message.folder_id.in_(folder_ids))
        async with self._db.session() as session:
            row = (await session.execute(stmt)).one()
            return (row[0], row[1])

    async def resolve_search_cursor(
        self, message_id: uuid.UUID, tokens: list[str], *, sort: SearchSort = "relevance",
    ) -> tuple[datetime | None, uuid.UUID, int | None] | None:
        """
        (received_at, id, tier) for a keyset cursor row -- tier only in
        "relevance" mode, since that is the only mode search_messages
        orders by it at all; a chronological cursor needs nothing but the
        two ordinary keyset columns, so tier is None rather than computed
        and then ignored.

        Args:
            message_id: The cursor row (GET /api/search's `before`)
            tokens: This search's tokens, from tokenize()
            sort: The mode the cursor is being resolved for -- must match
                the mode the page it continues was fetched under; see
                search_messages, which never mixes the two.

        Returns:
            None if the message does not exist (an invalid cursor)
        """
        async with self._db.session() as session:
            if sort == "relevance":
                tier = _match_tier(Message, tokens)
                stmt = select(Message.received_at, Message.id, tier).where(
                    Message.id == message_id
                )
                row = (await session.execute(stmt)).one_or_none()
                if row is None:
                    return None
                return (row[0], row[1], row[2])
            stmt = select(Message.received_at, Message.id).where(Message.id == message_id)
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            return (row[0], row[1], None)

    async def count_search_candidates(
        self,
        account_id: uuid.UUID | None,
        tokens: list[str],
        *,
        folder_ids: Sequence[uuid.UUID] | None = None,
        fields: frozenset[str] = SEARCH_FIELDS,
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        is_seen: bool | None = None,
    ) -> int:
        """
        An exact count over the same candidate predicate search_messages
        pages through -- computed once, not by summing pages, so the
        number a person sees does not move as they scroll. Sort mode
        never affects the candidate set, only its order, so this takes
        no sort argument.

        Args:
            account_id, tokens, folder_ids, fields: As in search_messages
            received_after, received_before, is_seen: As in search_messages

        Returns:
            0 when tokens has no lexemes at all
        """
        built = _build_candidate_query(
            account_id, tokens, folder_ids, fields,
            received_after=received_after, received_before=received_before,
            is_seen=is_seen,
        )
        if built is None:
            return 0
        _sub, msg = built
        async with self._db.session() as session:
            field_pred = _field_predicate(msg, tokens, fields)
            stmt = select(func.count(msg.id)).where(field_pred)
            return (await session.execute(stmt)).scalar_one()

    async def search_messages(
        self,
        account_id: uuid.UUID | None,
        tokens: list[str],
        *,
        folder_ids: Sequence[uuid.UUID] | None = None,
        fields: frozenset[str] = SEARCH_FIELDS,
        sort: SearchSort = "relevance",
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        is_seen: bool | None = None,
        cursor_received_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        cursor_tier: int | None = None,
        limit: int = 50,
    ) -> list[tuple[Message, str | None, int]]:
        """
        Field- and folder-scoped search, ranked by field tier then newest
        first (sort="relevance") or by date alone (sort="chronological"),
        keyset-paged.

        The recall stage is a prefix-AND tsquery over search_vector
        (subject, from_addr and body_text -- what PostIMAP's generated
        column indexes), not trigram similarity: a bare ILIKE over every
        body is already far cheaper than the trigram path this replaces,
        and the indexed tsquery path is thousands of times cheaper again.
        Every token is required, matched as a prefix so a partial word
        still finds a hit. Ranking is by explicit field tier (see
        _match_tier) rather than ts_rank, because search_vector carries
        no per-field weights and ts_rank provably cannot tell a subject
        hit from a body hit here.

        Unlike search_fulltext_with_snippet above (stemmed tsquery, ranked
        by relevance -- what the MCP search tool still uses), the 'simple'
        text search config applies no stemming at all, so a plural does
        not automatically match its singular -- deliberate, since once
        results are ranked by tier rather than by ts_rank, "matches" needs
        a single, predictable meaning.

        folder_ids is enforced here, in the query itself -- a caller
        filtering the returned page instead would silently turn a scoped
        search into an unscoped one with a smaller page, and would make
        keyset pagination lose rows across page boundaries. Same reason
        received_after/received_before narrow the candidate query rather
        than filtering the page.

        sort="chronological" drops tier from both the ORDER BY and the
        keyset predicate entirely -- a cursor produced under one mode is
        never valid to continue under the other, matching every other
        search-page control already: changing scope restarts pagination
        from scratch rather than reusing a cursor.

        A query with no lexemes at all (tokens == []) returns no rows --
        the caller (GET /api/search) is expected to have already checked
        this via tokenize() before running search_messages_fallback or
        count_search_candidates, rather than each of them repeating it.

        Args:
            account_id: Account scope, or None to search across every account
            tokens: This search's tokens, from tokenize()
            folder_ids: Restrict to these folders, or None for no restriction
            fields: Which of "subject"/"from"/"to"/"body" to search
            sort: "relevance" (tier then date, the default) or
                "chronological" (date alone, ranking tier ignored)
            received_after, received_before: Inclusive bounds on
                received_at; a message with no Date header matches
                neither and is excluded once either bound is given
            is_seen: Only read (True) or only unread (False) messages
            cursor_received_at, cursor_id, cursor_tier: Keyset cursor, from
                resolve_search_cursor under the same sort mode
            limit: Max rows

        Returns:
            (Message, snippet, tier) triples. In "relevance" mode ordered
            (tier ASC, received_at DESC NULLS LAST, id DESC); in
            "chronological" mode ordered (received_at DESC NULLS LAST, id
            DESC) and tier is still returned (for display) but plays no
            part in the order. snippet is None only when the matched text
            was empty.
        """
        built = _build_candidate_query(
            account_id, tokens, folder_ids, fields,
            received_after=received_after, received_before=received_before,
            is_seen=is_seen,
        )
        if built is None:
            return []
        _sub, msg = built

        async with self._db.session() as session:
            field_pred = _field_predicate(msg, tokens, fields)
            tier = _match_tier(msg, tokens)

            if sort == "chronological":
                stmt = (
                    select(msg, tier.label("tier"))
                    .where(field_pred)
                    .order_by(desc(msg.received_at).nulls_last(), desc(msg.id))
                )
                if cursor_id is not None:
                    stmt = stmt.where(
                        after_cursor(
                            msg.received_at, msg.id,
                            cursor_received_at, cursor_id, nulls_last=True,
                        )
                    )
            else:
                stmt = (
                    select(msg, tier.label("tier"))
                    .where(field_pred)
                    .order_by(tier, desc(msg.received_at).nulls_last(), desc(msg.id))
                )
                if cursor_id is not None:
                    stmt = stmt.where(
                        after_tier_cursor(
                            tier, msg.received_at, msg.id,
                            cursor_tier if cursor_tier is not None else 0,
                            cursor_received_at, cursor_id,
                        )
                    )
            stmt = stmt.limit(limit)

            result = await session.execute(stmt)
            return [
                (m, _build_snippet(_row_haystack(m, fields), tokens), tier_value)
                for m, tier_value in result.all()
            ]

    async def search_messages_fallback(
        self,
        account_id: uuid.UUID | None,
        tokens: list[str],
        *,
        folder_ids: Sequence[uuid.UUID] | None = None,
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        is_seen: bool | None = None,
        limit: int = 50,
    ) -> list[tuple[Message, str | None]]:
        """
        The trigram fallback tier: subject + from only, fired by the
        endpoint solely when search_messages' first page came back empty
        (and only for the first page -- a cursor never reaches this).
        Body is deliberately never trigram-matched here, which is the
        18-second-per-query cost the tsquery path above exists to avoid.
        Already newest-first with no tier of its own, so sort mode is not
        a parameter here -- a chronological search misses this stage
        exactly as often as a relevance one does.

        Args:
            account_id: Account scope, or None to search across every account
            tokens: This search's tokens, from tokenize()
            folder_ids: Restrict to these folders, or None for no restriction
            received_after, received_before, is_seen: As in search_messages
            limit: Max rows

        Returns:
            (Message, snippet) pairs, newest first. Empty when tokens is
            empty.
        """
        if not tokens:
            return []
        async with self._db.session() as session:
            # pg_trgm.word_similarity_threshold is a session GUC the %>
            # operator reads rather than an argument it takes -- SET
            # LOCAL scopes the 0.6 validated against real typos to this
            # transaction only. See _fallback_token_predicate for why the
            # operator form is required at all.
            await session.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.6"))

            stmt = select(Message).where(Message.expunged_at.is_(None))
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)
            if folder_ids is not None:
                stmt = stmt.where(Message.folder_id.in_(folder_ids))
            if received_after is not None:
                stmt = stmt.where(Message.received_at >= received_after)
            if received_before is not None:
                stmt = stmt.where(Message.received_at <= received_before)
            if is_seen is not None:
                stmt = stmt.where(Message.is_seen == is_seen)
            stmt = (
                stmt.where(*[_fallback_token_predicate(t) for t in tokens])
                .order_by(desc(Message.received_at).nulls_last(), desc(Message.id))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                (m, _build_snippet(f"{m.subject or ''} {m.from_addr or ''}", tokens))
                for m in result.scalars().all()
            ]


class VerdictRepository:
    """Repository for Verdict CRUD operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def create_verdict(
        self,
        mail_id: uuid.UUID,
        account_id: uuid.UUID,
        is_spam: bool,
        source: VerdictSource,
        *,
        message_id_hdr: str | None = None,
        model_used: str | None = None,
        reasoning: str | None = None,
    ) -> Verdict:
        """
        Create a new verdict for a message.

        msg_key and from_addr are derived here from the message row rather
        than accepted as parameters, so every call site gets the durability
        gate correctly populated without having to know how it's computed.

        Args:
            mail_id: Message this verdict applies to
            account_id: Account the message belongs to
            is_spam: Spam classification result
            source: How this verdict was produced
            message_id_hdr: RFC Message-ID header, copied at verdict time --
                the durability gate for source=ai keys on this, not on
                mail_id, since it must survive retention purge and resync
            model_used: AI model identifier
            reasoning: Explanation text

        Returns:
            Created Verdict
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(
                    Message.message_id,
                    Message.from_addr,
                    Message.subject,
                    Message.received_at,
                    Message.size_bytes,
                ).where(Message.id == mail_id)
            )
            row = result.one_or_none()

            if row is None:
                # The message is already gone (expunged and purged) by the
                # time a verdict is recorded for it -- keep the key stable
                # rather than losing the row to a NOT NULL violation.
                msg_key = message_id_hdr or f"legacy:{mail_id}"
                from_addr = None
            else:
                msg_key = compute_msg_key(
                    account_id=account_id,
                    message_id_hdr=message_id_hdr or row.message_id,
                    from_addr=row.from_addr,
                    subject=row.subject,
                    received_at=row.received_at,
                    size_bytes=row.size_bytes,
                )
                from_addr = row.from_addr

            verdict = Verdict(
                mail_id=mail_id,
                account_id=account_id,
                message_id_hdr=message_id_hdr,
                msg_key=msg_key,
                from_addr=from_addr,
                is_spam=is_spam,
                source=source,
                model_used=model_used,
                reasoning=reasoning,
            )
            session.add(verdict)
            await session.flush()
            await session.refresh(verdict)
            return verdict

    async def get_latest_for_mail(self, mail_id: uuid.UUID) -> Verdict | None:
        """
        Get the most recent verdict for a message.

        Args:
            mail_id: Message UUID

        Returns:
            Latest Verdict or None
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Verdict)
                .where(Verdict.mail_id == mail_id)
                .order_by(desc(Verdict.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def has_ai_verdict_for_msg_key(
        self,
        account_id: uuid.UUID,
        msg_key: str,
        from_addr: str | None,
    ) -> bool:
        """
        Check whether an AI verdict already exists for this durable
        message identity -- the never-classify-twice gate. Keyed on
        msg_key rather than message_id_hdr so a message with no
        Message-ID header (msg_key's hash fallback) is covered too, and
        `from_addr` is included so a sender forging the Message-ID of a
        message already verdicted cannot bypass classification.

        Args:
            account_id: Account scope
            msg_key: The durable key (see database/msg_key.py)
            from_addr: Envelope sender, or None

        Returns:
            True if a source=ai verdict already exists for this identity
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Verdict.id)
                .where(
                    Verdict.account_id == account_id,
                    Verdict.msg_key == msg_key,
                    Verdict.source == VerdictSource.AI,
                    Verdict.from_addr == from_addr if from_addr else Verdict.from_addr.is_(None),
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def get_current_verdict(self, mail_id: uuid.UUID) -> Verdict | None:
        """
        The verdict a caller should treat as current for a message: the
        latest user_feedback row if one exists, otherwise the latest
        ai/rule row. Never a naive "latest by created_at" across sources
        -- a user's correction must not be shadowed by a model call that
        was already in flight when they made it, however the two rows'
        timestamps happen to land (see pipeline/context.py's VerdictView,
        which this mirrors for the feedback listener's use).

        Args:
            mail_id: Message's current row id

        Returns:
            The current Verdict, or None if none has ever been recorded
        """
        async with self._db.session() as session:
            feedback_result = await session.execute(
                select(Verdict)
                .where(Verdict.mail_id == mail_id, Verdict.source == VerdictSource.USER_FEEDBACK)
                .order_by(desc(Verdict.created_at))
                .limit(1)
            )
            row = feedback_result.scalar_one_or_none()
            if row is not None:
                return row
            other_result = await session.execute(
                select(Verdict)
                .where(Verdict.mail_id == mail_id, Verdict.source != VerdictSource.USER_FEEDBACK)
                .order_by(desc(Verdict.created_at))
                .limit(1)
            )
            return other_result.scalar_one_or_none()

    async def get_stats(
        self,
        account_id: uuid.UUID,
    ) -> dict[str, int]:
        """
        Get verdict statistics for an account.

        Args:
            account_id: Account scope (joins through Message)

        Returns:
            Dict with keys: total, spam, ham
        """
        async with self._db.session() as session:
            stmt = (
                select(
                    func.count(Verdict.id).label("total"),
                    func.count(Verdict.id).filter(Verdict.is_spam.is_(True)).label("spam"),
                    func.count(Verdict.id).filter(Verdict.is_spam.is_(False)).label("ham"),
                )
                .join(Message, Verdict.mail_id == Message.id)
                .where(Message.account_id == account_id)
            )
            result = await session.execute(stmt)
            row = result.one()
            return {
                "total": row.total,
                "spam": row.spam,
                "ham": row.ham,
            }


class FolderRepository:
    """Repository for Folder read operations.

    PostIMAP handles folder creation and sync state updates.
    This repository provides read access and preference management.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_by_account(self, account_id: uuid.UUID) -> list[Folder]:
        """
        Get all folders for an account.

        Args:
            account_id: Account UUID

        Returns:
            List of Folder objects
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Folder).where(Folder.account_id == account_id)
            )
            return list(result.scalars().all())

    async def get_by_id(self, folder_id: uuid.UUID) -> Folder | None:
        """
        Get a folder by ID.

        Args:
            folder_id: Folder UUID

        Returns:
            Folder if found, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Folder).where(Folder.id == folder_id)
            )
            return result.scalar_one_or_none()

    async def get_by_imap_name(
        self,
        account_id: uuid.UUID,
        imap_name: str,
    ) -> Folder | None:
        """
        Get a folder by IMAP name within an account.

        Args:
            account_id: Account scope
            imap_name: IMAP folder path

        Returns:
            Folder if found, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Folder).where(
                    Folder.account_id == account_id,
                    Folder.imap_name == imap_name,
                )
            )
            return result.scalar_one_or_none()

    async def get_effective_special_use(self, folder_id: uuid.UUID) -> str | None:
        """
        Get a folder's effective special_use: folder_prefs override, or the raw value.

        folder_prefs.special_use_override exists for servers that don't
        advertise SPECIAL-USE -- reading Folder.special_use raw here would
        make a folder that only has an override invisible to this check.

        Args:
            folder_id: Folder UUID

        Returns:
            Effective special_use role, or None if unset either way
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(func.coalesce(FolderPrefs.special_use_override, Folder.special_use))
                .select_from(Folder)
                .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
                .where(Folder.id == folder_id)
            )
            return result.scalar_one_or_none()

    async def resolve_special_folder(
        self, account_id: uuid.UUID, role: str,
    ) -> uuid.UUID | None:
        """
        Resolve a folder by its effective special_use (override or raw),
        falling back to matching a well-known name for the role when no
        folder carries the flag at all.

        The name fallback only runs when the flag match finds nothing --
        a folder correctly flagged always wins, so this can only add a
        result, never redirect one away from a server-declared folder.

        Args:
            account_id: Account to look up
            role: Folder role key (e.g., "archive", "junk", "trash", "inbox")

        Returns:
            Folder UUID or None if no folder has that effective role and
            none matches a name fallback for it
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Folder.id)
                .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
                .where(
                    Folder.account_id == account_id,
                    func.coalesce(FolderPrefs.special_use_override, Folder.special_use) == role,
                )
                .limit(1)
            )
            folder_id = result.scalar_one_or_none()
            if folder_id is not None:
                return folder_id

            names = _ROLE_NAME_FALLBACKS.get(role)
            if not names:
                return None
            # imap_name is the full IMAP path (contract: "insert-only"),
            # so a namespaced mailbox -- INBOX.Archive on a Dovecot with
            # an INBOX namespace, INBOX/Archive elsewhere -- never matched
            # the bare candidate names above; match the last path segment
            # instead, whichever of the two common delimiters produced
            # it. Ordered by the tuple's own preference (first name
            # wins), then by id, so two matching folders (Junk and Spam
            # both present) resolve the same way on every call rather
            # than whatever the planner returns.
            last_segment = func.regexp_replace(
                func.lower(Folder.imap_name), r"^.*[./]", "",
            )
            priority = case(
                *[(last_segment == name, i) for i, name in enumerate(names)],
                else_=len(names),
            )
            result = await session.execute(
                select(Folder.id)
                .where(
                    Folder.account_id == account_id,
                    Folder.deleted_at.is_(None),
                    last_segment.in_(names),
                )
                .order_by(priority, Folder.id)
                .limit(1)
            )
            return result.scalar_one_or_none()


class RowMarks(NamedTuple):
    """What a list row shows beside its sender and subject."""

    has_attachments: bool
    verdict_is_spam: bool | None


async def list_row_marks(
    session: AsyncSession, message_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, RowMarks]:
    """
    The paperclip and spam marks for a page of list rows, in two reads
    for the whole page rather than two per row.

    has_attachments counts every attachment row, the same set the reading
    pane lists. verdict_is_spam is the latest verdict by created_at, the
    one the reading pane shows (VerdictRepository.get_latest_for_mail);
    None when the message has never been classified.

    Returns:
        A mark for every id given
    """
    ids = list(message_ids)
    if not ids:
        return {}
    attached = set(
        (
            await session.execute(
                select(Attachment.message_id).where(Attachment.message_id.in_(ids)).distinct()
            )
        ).scalars()
    )
    latest_rows = (
        await session.execute(
            select(Verdict.mail_id, Verdict.is_spam)
            .where(Verdict.mail_id.in_(ids))
            .order_by(Verdict.mail_id, desc(Verdict.created_at))
            .distinct(Verdict.mail_id)
        )
    ).all()
    latest = {mail_id: is_spam for mail_id, is_spam in latest_rows}
    return {mid: RowMarks(mid in attached, latest.get(mid)) for mid in ids}


class SyncNotificationRepository:
    """Repository for sync_notifications read operations.

    Writes go through postimap/actions.py -- acknowledged_at is the only
    consumer-writable column and even that is a contract write, not a plain
    UPDATE issued from here.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def list_for_account(
        self, account_id: uuid.UUID, *, unacknowledged_only: bool = False, limit: int = 100,
    ) -> list[SyncNotification]:
        """
        List notifications for an account, newest first.

        Args:
            account_id: Account to list notifications for
            unacknowledged_only: Only rows with acknowledged_at IS NULL --
                the query the partial index on this table exists for
            limit: Maximum rows to return

        Returns:
            SyncNotification rows, newest first
        """
        async with self._db.session() as session:
            stmt = (
                select(SyncNotification)
                .where(SyncNotification.account_id == account_id)
                # id as a tiebreaker: two rows written in the same transaction
                # share PostgreSQL's transaction-frozen now(), so created_at
                # alone leaves their order unspecified.
                .order_by(desc(SyncNotification.created_at), desc(SyncNotification.id))
                .limit(limit)
            )
            if unacknowledged_only:
                stmt = stmt.where(SyncNotification.acknowledged_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def unacknowledged_count(self, account_id: uuid.UUID) -> int:
        """Count of unacknowledged notifications for an account -- a bell badge."""
        async with self._db.session() as session:
            result = await session.execute(
                select(func.count(SyncNotification.id)).where(
                    SyncNotification.account_id == account_id,
                    SyncNotification.acknowledged_at.is_(None),
                )
            )
            return result.scalar_one()

    async def list_all(
        self, *, unacknowledged_only: bool = False, limit: int = 100,
    ) -> list[SyncNotification]:
        """
        Every account's notifications in one list, newest first -- inactive
        accounts included, since the folder-delete guard a notification
        can block is account-wide whether the account is active or not.

        Args:
            unacknowledged_only: Only rows with acknowledged_at IS NULL
            limit: Maximum rows to return

        Returns:
            SyncNotification rows, newest first
        """
        async with self._db.session() as session:
            stmt = (
                select(SyncNotification)
                .order_by(desc(SyncNotification.created_at), desc(SyncNotification.id))
                .limit(limit)
            )
            if unacknowledged_only:
                stmt = stmt.where(SyncNotification.acknowledged_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def unacknowledged_count_all(self) -> int:
        """Unacknowledged notifications across every account, inactive ones included."""
        async with self._db.session() as session:
            result = await session.execute(
                select(func.count(SyncNotification.id)).where(
                    SyncNotification.acknowledged_at.is_(None),
                )
            )
            return result.scalar_one()


class AttachmentRepository:
    """Repository for Attachment read operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def get_by_message_id(self, message_id: uuid.UUID) -> list[Attachment]:
        """
        Get all attachments for a message.

        Args:
            message_id: Parent message UUID

        Returns:
            List of Attachment objects
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Attachment).where(Attachment.message_id == message_id)
            )
            return list(result.scalars().all())


class TagRepository:
    """Repository for MailTag CRUD operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def add_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
        source: TagSource,
    ) -> MailTag:
        """
        Add a tag to a message (idempotent via upsert).

        Args:
            mail_id: Message to tag (FK column is still mail_id)
            tag_name: Tag string
            source: Where this tag came from

        Returns:
            The MailTag (existing or new)
        """
        values: dict[str, Any] = {
            "mail_id": mail_id,
            "tag_name": tag_name,
            "source": source,
        }
        async with self._db.session() as session:
            stmt = (
                pg_insert(MailTag)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_mail_tag")
                .returning(MailTag)
            )
            result = await session.execute(stmt)
            tag = result.scalar_one_or_none()
            if tag is not None:
                return tag

            # Already existed, fetch it
            fetch = await session.execute(
                select(MailTag).where(
                    MailTag.mail_id == mail_id,
                    MailTag.tag_name == tag_name,
                )
            )
            return fetch.scalar_one()

    async def remove_tag(
        self,
        mail_id: uuid.UUID,
        tag_name: str,
    ) -> bool:
        """
        Remove a tag from a message.

        Args:
            mail_id: Message UUID (FK column name)
            tag_name: Tag to remove

        Returns:
            True if tag was removed, False if not found
        """
        async with self._db.session() as session:
            stmt = delete(MailTag).where(
                MailTag.mail_id == mail_id,
                MailTag.tag_name == tag_name,
            )
            result = await session.execute(stmt)
            return bool(result.rowcount > 0)  # type: ignore[attr-defined]

    async def get_tags_for_mail(self, mail_id: uuid.UUID) -> list[MailTag]:
        """
        Get all tags for a message.

        Args:
            mail_id: Message UUID (FK column name)

        Returns:
            List of MailTag objects
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(MailTag).where(MailTag.mail_id == mail_id)
            )
            return list(result.scalars().all())


class AlertRepository:
    """Repository for the alerts table -- both the durable in-app list and,
    for a mail alert, the fires-exactly-once dedup gate (see Alert's own
    docstring). Owned table, no foreign key onto anything of PostIMAP's."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def create_mail_alert(
        self,
        *,
        account_id: uuid.UUID,
        message_id: uuid.UUID,
        msg_key: str,
        title: str | None,
        body: str | None,
        folder_id: uuid.UUID | None = None,
        delivered: bool = True,
    ) -> Alert | None:
        """
        Insert a "new mail" alert.

        Delivered immediately by default -- the in-app path has no
        separate dispatch phase of its own (no push subscriptions to
        notify, no VAPID keys), so delivered_at is stamped at insert time
        rather than left for a later pass to claim. `delivered=False` is
        for a message whose final folder is not yet known (see
        alerts/dispatch.py's own docstring on staging vs. immediate
        delivery): the row exists, but delivered_at stays NULL and
        neither list_recent nor unseen_count see it until something calls
        finalize_pending_mail_alerts() and stamps delivered_at itself.

        dedupe_key embeds msg_key rather than message_id, the same reason
        Verdict and MessageEmbedding do: a UIDVALIDITY resync replaces
        every messages.id in a folder, and the row that must never fire
        twice has to survive that. ON CONFLICT DO NOTHING is the entire
        "fires exactly once" mechanism -- ordinary insert, not an upsert,
        whether the row lands delivered or staged.

        Args:
            account_id, message_id: Source coordinates for the alert's URL
            msg_key: The durable key (database/msg_key.py) -- what
                dedupe_key is built from
            title, body: Rendered once here (subject, sender) rather than
                resolved again by every reader of the alert list
            folder_id: What a folder filter (list_recent, unseen_count)
                scopes against -- the same folder the live event and the
                push dispatch already carry. For a staged row this is the
                arrival folder, a placeholder overwritten with the real
                one at finalization.
            delivered: False stages the row instead of delivering it

        Returns:
            The inserted Alert, or None if an alert for this msg_key
            already exists (a resync, not a new arrival)
        """
        dedupe_key = f"mail:{account_id}:{msg_key}"
        now = func.now()
        async with self._db.session() as session:
            stmt = (
                pg_insert(Alert)
                .values(
                    kind="mail",
                    deliver_at=now,
                    delivered_at=now if delivered else None,
                    title=title,
                    body=body,
                    url=f"/?message={message_id}",
                    dedupe_key=dedupe_key,
                    account_id=account_id,
                    message_id=message_id,
                    folder_id=folder_id,
                )
                .on_conflict_do_nothing(constraint="uq_alerts_dedupe_key")
                .returning(Alert)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def create_outbox_stalled_alert(
        self, *, account_id: uuid.UUID, dedupe_key: str, title: str, body: str,
    ) -> Alert | None:
        """
        Insert a delivered "outbox_stalled" alert -- see outbox/stalled.py.

        No folder_id, so no folder filter ever hides it: a message stuck on
        its way out matters whichever folders a device alerts for. The same
        ON CONFLICT DO NOTHING on dedupe_key as a mail alert is what makes
        it fire once per stuck row.

        Returns:
            The inserted Alert, or None if this row was already alerted on
        """
        now = func.now()
        async with self._db.session() as session:
            stmt = (
                pg_insert(Alert)
                .values(
                    kind="outbox_stalled",
                    deliver_at=now,
                    delivered_at=now,
                    title=title,
                    body=body,
                    dedupe_key=dedupe_key,
                    account_id=account_id,
                )
                .on_conflict_do_nothing(constraint="uq_alerts_dedupe_key")
                .returning(Alert)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list_recent(
        self, *, limit: int = 50, folder_ids: list[uuid.UUID] | None = None,
        unseen_only: bool = False,
    ) -> list[Alert]:
        """
        The durable alert list, newest first -- delivered_at rather than
        deliver_at, since a future-dated reminder alert (not built by this
        block) has not happened yet and does not belong in this list until
        it has.

        Args:
            limit: Max rows
            folder_ids: The same "which folders alert" preference the SSE
                and push paths already compute (use-push.ts's
                useEffectiveAlertFolderIds) -- None (the caller's own
                folder scope is unrestricted) skips the filter entirely;
                a list, possibly empty, restricts to alerts whose
                folder_id is in it. A row with no folder_id (a reminder,
                or one that predates the column) always passes, since a
                folder preference has nothing to say about it.
            unseen_only: Only rows with dismissed_at IS NULL -- what the
                bell badge (unseen_count) itself counts, so a caller that
                needs its list and its count to agree can ask for exactly
                that rather than a plain recent page that may not even
                contain every unseen row.

        Returns:
            Alerts ordered (delivered_at DESC, id DESC), delivered only
        """
        async with self._db.session() as session:
            stmt = select(Alert).where(Alert.delivered_at.is_not(None))
            if unseen_only:
                stmt = stmt.where(Alert.dismissed_at.is_(None))
            if folder_ids is not None:
                stmt = stmt.where(
                    or_(Alert.folder_id.is_(None), Alert.folder_id.in_(folder_ids)),
                )
            stmt = stmt.order_by(desc(Alert.delivered_at), desc(Alert.id)).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def unseen_counts_by_kind(
        self, *, folder_ids: list[uuid.UUID] | None = None, arrival_folders_only: bool = False,
    ) -> dict[str, int]:
        """
        How many delivered alerts of each kind have not been dismissed yet
        -- what the bell's badge is computed from, per kind since not every
        kind is new mail. folder_ids is the same filter list_recent takes,
        for the same reason: the badge and the list it counts must agree
        on what's in scope.

        arrival_folders_only scopes to the folders mail arrives in instead
        -- what a push subscription with no alert_folder_ids of its own is
        pushed for (PushSubscriptionRepository.list_for_alert), so a device
        badge counts what that device was notified about. A row with no
        folder_id always passes either filter.
        """
        async with self._db.session() as session:
            stmt = (
                select(Alert.kind, func.count(Alert.id))
                .where(Alert.delivered_at.is_not(None), Alert.dismissed_at.is_(None))
                .group_by(Alert.kind)
            )
            if folder_ids is not None:
                stmt = stmt.where(
                    or_(Alert.folder_id.is_(None), Alert.folder_id.in_(folder_ids)),
                )
            if arrival_folders_only:
                stmt = stmt.where(
                    or_(Alert.folder_id.is_(None), _is_arrival_folder(Alert.folder_id)),
                )
            return {kind: count for kind, count in (await session.execute(stmt)).all()}

    async def lookup(self, alert_ids: Sequence[uuid.UUID]) -> list[Alert]:
        """The alerts among these ids that still exist, in any state -- a
        device holding shown notifications asks which of them are still
        worth showing."""
        if not alert_ids:
            return []
        async with self._db.session() as session:
            stmt = select(Alert).where(Alert.id.in_(list(alert_ids)))
            return list((await session.execute(stmt)).scalars().all())

    async def recently_resolved_mail(
        self, *, limit: int, within: timedelta,
    ) -> list[uuid.UUID]:
        """
        Mail alerts dismissed or resolved within the given window, newest
        first -- what a native push carries so a phone withdraws banners
        for mail already read elsewhere.
        """
        async with self._db.session() as session:
            stmt = (
                select(Alert.id)
                .where(
                    Alert.kind == "mail",
                    Alert.dismissed_at > func.now() - within,
                )
                .order_by(desc(Alert.dismissed_at), desc(Alert.id))
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def unseen_count(self, *, folder_ids: list[uuid.UUID] | None = None) -> int:
        """Every kind's unseen_counts_by_kind, added up."""
        return sum((await self.unseen_counts_by_kind(folder_ids=folder_ids)).values())

    async def dismiss(self, alert_id: uuid.UUID) -> bool:
        """
        Mark one alert dismissed. Idempotent -- dismissing an
        already-dismissed alert (two browsers, the same click twice) is
        not an error, it does nothing on the second call.

        Returns:
            True if this call is what dismissed it, False if it was
            already dismissed or does not exist
        """
        async with self._db.session() as session:
            stmt = (
                update(Alert)
                .where(Alert.id == alert_id, Alert.dismissed_at.is_(None))
                .values(dismissed_at=func.now())
            )
            result = await session.execute(stmt)
            return bool(result.rowcount > 0)  # type: ignore[attr-defined]

    async def dismiss_all(self, *, kinds: list[str] | None = None) -> int:
        """Mark every currently-undismissed, delivered alert dismissed.

        Args:
            kinds: Only alerts of these kinds; None dismisses every kind

        Returns:
            The number of rows this call dismissed
        """
        async with self._db.session() as session:
            stmt = (
                update(Alert)
                .where(Alert.delivered_at.is_not(None), Alert.dismissed_at.is_(None))
                .values(dismissed_at=func.now())
            )
            if kinds is not None:
                stmt = stmt.where(Alert.kind.in_(kinds))
            result = await session.execute(stmt)
            return int(result.rowcount)  # type: ignore[attr-defined]


def _is_arrival_folder(folder_id: Any) -> Any:
    """SQL: the folder is one mail arrives in -- its effective special use
    unset or "inbox" (a folder no longer mirrored counts, since nothing
    says otherwise). The default scope of a device that never narrowed
    its own, the same as isArrivalFolder in the web's alert-prefs.ts."""
    effective_special_use = (
        select(func.coalesce(FolderPrefs.special_use_override, Folder.special_use))
        .select_from(Folder)
        .outerjoin(FolderPrefs, Folder.id == FolderPrefs.folder_id)
        .where(Folder.id == folder_id)
        .scalar_subquery()
    )
    return or_(effective_special_use.is_(None), effective_special_use == "inbox")


class PushSubscriptionRepository:
    """Repository for push_subscriptions -- one device's push registration,
    browser or native app, and the per-device preferences that ride along
    on the same row (see the model's own docstring for why)."""

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        label: str | None,
    ) -> PushSubscription:
        """
        Register a subscription, or refresh one already registered at
        this endpoint -- a browser re-subscribing after clearing its own
        storage, or PushManager rotating the endpoint under an
        unchanged registration, both look like this rather than a
        second device appearing.

        Returns:
            The subscription row, existing or newly inserted
        """
        async with self._db.session() as session:
            stmt = (
                pg_insert(PushSubscription)
                .values(
                    endpoint=endpoint, p256dh=p256dh, auth=auth, label=label,
                    last_seen_at=func.now(),
                )
                .on_conflict_do_update(
                    index_elements=["endpoint"],
                    set_={
                        "p256dh": p256dh,
                        "auth": auth,
                        "failed_at": None,
                        "last_seen_at": func.now(),
                    },
                )
                .returning(PushSubscription)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def upsert_native(
        self,
        *,
        installation_id: uuid.UUID,
        relay_url: str,
        encrypted_relay_ticket: bytes,
        encrypted_content_key: bytes,
        label: str | None,
        muted_channels: list[str] | None,
    ) -> PushSubscription:
        """
        Register a native device, or refresh the one already registered
        under this installation id -- the app re-registers whenever its
        device token or relay ticket changes, and at least daily.

        The relay address, ticket and key always take the new values. A
        label or muted_channels of None leaves a registered device's own
        value alone (a new device starts with no label and nothing muted),
        so a refresh never undoes a change made from another client.

        Returns:
            The subscription row, existing or newly inserted
        """
        refreshed: dict[str, Any] = {
            "relay_url": relay_url,
            "encrypted_relay_ticket": encrypted_relay_ticket,
            "encrypted_content_key": encrypted_content_key,
            "failed_at": None,
            "last_seen_at": func.now(),
        }
        if label is not None:
            refreshed["label"] = label
        if muted_channels is not None:
            refreshed["muted_channels"] = muted_channels
        async with self._db.session() as session:
            stmt = (
                pg_insert(PushSubscription)
                .values(
                    transport="apns", installation_id=installation_id, relay_url=relay_url,
                    encrypted_relay_ticket=encrypted_relay_ticket,
                    encrypted_content_key=encrypted_content_key, label=label,
                    muted_channels=muted_channels or [], last_seen_at=func.now(),
                )
                .on_conflict_do_update(
                    index_elements=["installation_id"],
                    index_where=PushSubscription.installation_id.is_not(None),
                    set_=refreshed,
                )
                .returning(PushSubscription)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def list_native(self) -> list[PushSubscription]:
        """Every native (apns) device -- what a read-sync push goes to."""
        async with self._db.session() as session:
            stmt = select(PushSubscription).where(PushSubscription.transport == "apns")
            return list((await session.execute(stmt)).scalars().all())

    async def list_all(self) -> list[PushSubscription]:
        """Every registered device, newest first -- the Settings page's
        own device list."""
        async with self._db.session() as session:
            stmt = select(PushSubscription).order_by(desc(PushSubscription.created_at))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get(self, subscription_id: uuid.UUID) -> PushSubscription | None:
        async with self._db.session() as session:
            stmt = select(PushSubscription).where(PushSubscription.id == subscription_id)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def update_prefs(
        self,
        subscription_id: uuid.UUID,
        *,
        alert_folder_ids: list[uuid.UUID] | None | Literal["unset"] = "unset",
        reminders_enabled: bool | None = None,
        label: str | None | Literal["unset"] = "unset",
        muted_channels: list[str] | None = None,
    ) -> PushSubscription | None:
        """
        Update one device's own preferences. Every argument defaults to
        leaving its column untouched (`"unset"`/`None` sentinels) rather
        than requiring the caller to re-send the whole row -- a PATCH
        naming only `reminders_enabled` must not silently null out
        `alert_folder_ids`.

        Returns:
            The updated row, or None if subscription_id does not exist
        """
        values: dict[str, Any] = {}
        if alert_folder_ids != "unset":
            values["alert_folder_ids"] = alert_folder_ids
        if reminders_enabled is not None:
            values["reminders_enabled"] = reminders_enabled
        if label != "unset":
            values["label"] = label
        if muted_channels is not None:
            values["muted_channels"] = muted_channels
        if not values:
            return await self.get(subscription_id)
        async with self._db.session() as session:
            stmt = (
                update(PushSubscription)
                .where(PushSubscription.id == subscription_id)
                .values(**values)
                .returning(PushSubscription)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def delete(self, subscription_id: uuid.UUID) -> bool:
        """
        Unregister a device -- the browser's own unsubscribe, or a stale
        endpoint a push send discovered is gone (see push/send.py).

        Returns:
            True if a row was deleted
        """
        async with self._db.session() as session:
            stmt = delete(PushSubscription).where(PushSubscription.id == subscription_id)
            result = await session.execute(stmt)
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def mark_seen(self, subscription_id: uuid.UUID) -> None:
        """A push to this subscription succeeded -- clear any prior
        failure and refresh last_seen_at."""
        async with self._db.session() as session:
            await session.execute(
                update(PushSubscription)
                .where(PushSubscription.id == subscription_id)
                .values(last_seen_at=func.now(), failed_at=None)
            )

    async def mark_failed(self, subscription_id: uuid.UUID) -> None:
        """A push to this subscription failed for a reason other than the
        protocol's own unsubscribe signal (404/410, handled by deleting
        the row instead) -- stamped rather than retried, since an alert is
        time-sensitive and the next arrival will simply try again."""
        async with self._db.session() as session:
            await session.execute(
                update(PushSubscription)
                .where(PushSubscription.id == subscription_id)
                .values(failed_at=func.now())
            )

    async def list_for_alert(
        self, *, kind: str, folder_id: uuid.UUID | None,
    ) -> list[PushSubscription]:
        """
        Subscriptions eligible to receive a given alert -- the server-side
        filter that makes "which folders alert" and "do reminders alert
        at all" real per-device preferences rather than a client-side
        approximation (see the model's own docstring).

        Args:
            kind: "mail" or "reminder"
            folder_id: The mail alert's folder, for the alert_folder_ids
                filter. Ignored for "reminder", which has no folder to
                filter on. A subscription with no explicit alert_folder_ids
                only matches an arrival folder (special_use effectively
                unset or "inbox") -- the same default the client resolves
                to for a browser with no subscription yet (see
                isArrivalFolder in alert-prefs.ts) -- so a freshly
                registered device isn't pushed its own Sent, Drafts,
                Trash and Junk. A "mail" alert with no folder_id (the
                originating message already gone) only reaches a
                subscription that alerts for every folder -- a scoped
                subscription cannot confirm a folder it was never told,
                and neither can a null-scoped one confirm an unknown
                folder is an arrival folder.

        A device that muted the alert's channel (push/channels.py) is left
        out whatever the kind.
        """
        async with self._db.session() as session:
            stmt = select(PushSubscription).where(
                not_(PushSubscription.muted_channels.any(channel_for_kind(kind))),  # type: ignore[arg-type]
            )
            if kind == "mail":
                if folder_id is not None:
                    stmt = stmt.where(
                        or_(
                            PushSubscription.alert_folder_ids.any(folder_id),  # type: ignore[arg-type]
                            and_(
                                PushSubscription.alert_folder_ids.is_(None),
                                _is_arrival_folder(folder_id),
                            ),
                        )
                    )
                else:
                    stmt = stmt.where(PushSubscription.alert_folder_ids.is_(None))
            elif kind == "reminder":
                stmt = stmt.where(PushSubscription.reminders_enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())
