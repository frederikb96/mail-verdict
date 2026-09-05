"""
Repository layer for database operations.

All queries are account_id scoped for multi-account isolation.
PostIMAP owns message ingestion — MailVerdict reads/queries messages
and manages its own tables (verdicts, tags, prefs, settings).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, case, cast, delete, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from mail_verdict.core.cursor import after_cursor
from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Attachment,
    Folder,
    FolderPrefs,
    MailTag,
    Message,
    SyncNotification,
    TagSource,
    Verdict,
    VerdictSource,
)
from mail_verdict.database.msg_key import compute_msg_key

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
            **kwargs: Fields to update (unified_name, is_visible,
                      display_name, special_use_override)

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


def _search_haystack(fields: frozenset[str]) -> Any:
    """One concatenated text expression over exactly the requested fields,
    space-joined so a token can never straddle two fields it shouldn't."""
    columns: list[Any] = []
    if "subject" in fields:
        columns.append(func.coalesce(Message.subject, ""))
    if "from" in fields:
        columns.append(func.coalesce(Message.from_addr, ""))
    if "to" in fields:
        columns.append(func.coalesce(cast(Message.to_addrs, Text), ""))
    if "body" in fields:
        columns.append(func.coalesce(Message.body_text, ""))
    if not columns:
        columns = [cast("", Text)]
    expr = columns[0]
    for col in columns[1:]:
        expr = expr + " " + col
    return expr


def _ilike_escape(token: str) -> str:
    """Escape ILIKE's own wildcards in raw user input before wrapping it
    in %...% -- a query containing a literal % or _ must match that
    character, not be treated as a pattern."""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fuzzy_token_predicate(haystack: Any, token: str, threshold: float = 0.4) -> Any:
    """A token matches if it appears literally, or -- pg_trgm's
    word_similarity, not similarity() -- if a substring of the (possibly
    much longer) haystack is a near enough match to the short token that a
    typo shouldn't lose the hit. similarity() compares two whole strings
    and would systematically under-score a five-letter token against a
    paragraph; word_similarity is the one meant for exactly this shape.

    An '@' token is left literal-only. Measured directly: two unrelated
    addresses sharing a domain score *higher* on word_similarity
    ("bob@example.com" against "alice@example.com": 0.75) than a genuine
    one-letter typo does ("reimbursment" against "reimbursement": 0.69) --
    there is no threshold that keeps the second and drops the first. An
    address is a literal identifier a user is typing exactly, not prose
    worth typo-tolerance over.
    """
    escaped = _ilike_escape(token)
    if "@" in token:
        return haystack.ilike(f"%{escaped}%")
    return or_(
        haystack.ilike(f"%{escaped}%"),
        func.word_similarity(token, haystack) >= threshold,
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

    async def search_fulltext(
        self,
        account_id: uuid.UUID,
        query: str,
        *,
        limit: int = 50,
        fuzzy: bool = False,
        similarity_threshold: float = 0.3,
    ) -> list[Message]:
        """
        Full-text search on subject + body_text using tsvector.

        Falls back to pg_trgm similarity for fuzzy matching.

        Args:
            account_id: Account scope
            query: Search query string
            limit: Max results
            fuzzy: Enable pg_trgm fuzzy matching
            similarity_threshold: Minimum trigram similarity score

        Returns:
            Messages ranked by relevance
        """
        # 'simple' matches the config PostIMAP's search_vector generated
        # column is built with (see the search_vector column docstring in
        # database/models.py); an 'english' query config would silently
        # under-match against a 'simple' index config.
        async with self._db.session() as session:
            ts_query = func.websearch_to_tsquery("simple", query)

            if fuzzy:
                # Combined: tsvector rank + trigram similarity
                rank = func.ts_rank(Message.search_vector, ts_query)
                trgm_sim = func.similarity(Message.subject, query)
                stmt = (
                    select(Message)
                    .where(
                        Message.account_id == account_id,
                        Message.expunged_at.is_(None),
                        (Message.search_vector.op("@@")(ts_query))
                        | (func.similarity(Message.subject, query) >= similarity_threshold)
                        | (func.similarity(Message.body_text, query) >= similarity_threshold),
                    )
                    .order_by(desc(rank + trgm_sim))
                    .limit(limit)
                )
            else:
                rank = func.ts_rank(Message.search_vector, ts_query)
                stmt = (
                    select(Message)
                    .where(
                        Message.account_id == account_id,
                        Message.expunged_at.is_(None),
                        Message.search_vector.op("@@")(ts_query),
                    )
                    .order_by(desc(rank))
                    .limit(limit)
                )

            result = await session.execute(stmt)
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

    async def search_messages(
        self,
        account_id: uuid.UUID | None,
        query: str,
        *,
        folder_ids: Sequence[uuid.UUID] | None = None,
        fields: frozenset[str] = SEARCH_FIELDS,
        cursor_received_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[tuple[Message, str | None]]:
        """
        Fuzzy, field- and folder-scoped search, newest first, keyset-paged.

        Unlike search_fulltext_with_snippet above (stemmed tsquery, ranked
        by relevance -- what the MCP search tool still uses), this treats
        every whitespace-split token of the query as required, matched
        against one concatenated haystack built from exactly the toggled
        fields: a literal substring, or -- pg_trgm's word_similarity --
        close enough that a typo doesn't lose the hit. That is deliberately
        not stemmed-tsquery recall (a plural does not automatically match
        its singular): once results are ordered by date rather than
        relevance, "matches" needs a single, predictable meaning rather
        than one that silently depends on ts_rank.

        folder_ids is enforced here, in the query itself -- a caller
        filtering the returned page instead would silently turn a scoped
        search into an unscoped one with a smaller page, and would make
        keyset pagination lose rows across page boundaries.

        Args:
            account_id: Account scope, or None to search across every account
            query: Raw query text
            folder_ids: Restrict to these folders, or None for no restriction
            fields: Which of "subject"/"from"/"to"/"body" to search
            cursor_received_at, cursor_id: Keyset cursor (core/cursor.py)
            limit: Max rows

        Returns:
            (Message, snippet) pairs ordered received_at DESC, id DESC.
            snippet is None only when the matched text was empty.
        """
        tokens = query.split()
        if not tokens:
            return []

        haystack = _search_haystack(fields)
        async with self._db.session() as session:
            base = select(Message, haystack.label("haystack")).where(
                Message.expunged_at.is_(None)
            )
            if account_id is not None:
                base = base.where(Message.account_id == account_id)
            if folder_ids is not None:
                base = base.where(Message.folder_id.in_(folder_ids))
            sub = base.subquery()
            msg = aliased(Message, sub)

            stmt = (
                select(msg, sub.c.haystack)
                .where(*[_fuzzy_token_predicate(sub.c.haystack, t) for t in tokens])
                # NULLS LAST: a message with no received_at (no date
                # header at all) belongs at the bottom of a newest-first
                # list, not pinned above every dated result the way
                # Postgres's own DESC default would place it.
                .order_by(desc(sub.c.received_at).nulls_last(), desc(sub.c.id))
            )
            if cursor_id is not None:
                stmt = stmt.where(
                    after_cursor(
                        msg.received_at, msg.id, cursor_received_at, cursor_id, nulls_last=True,
                    )
                )
            stmt = stmt.limit(limit)

            result = await session.execute(stmt)
            return [
                (m, _build_snippet(haystack_text, tokens))
                for m, haystack_text in result.all()
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
