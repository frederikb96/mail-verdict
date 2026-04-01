"""
Repository layer for database operations.

All queries are account_id scoped for multi-account isolation.
PostIMAP owns message ingestion — MailVerdict reads/queries messages
and manages its own tables (verdicts, tags, prefs, settings).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import (
    Account,
    AccountPrefs,
    Attachment,
    Folder,
    FolderPrefs,
    MailTag,
    Message,
    TagSource,
    Verdict,
    VerdictSource,
)

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection


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
            **kwargs: Fields to update (emoji, spam_enabled,
                      embedding_lookback_days, folder_mapping, folder_order)

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
                      subscribed, display_name)

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

        Excludes soft-deleted messages (deleted_at IS NOT NULL).

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
                    Message.deleted_at.is_(None),
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
        async with self._db.session() as session:
            ts_query = func.plainto_tsquery("english", query)

            if fuzzy:
                # Combined: tsvector rank + trigram similarity
                rank = func.ts_rank(Message.search_vector, ts_query)
                trgm_sim = func.similarity(Message.subject, query)
                stmt = (
                    select(Message)
                    .where(
                        Message.account_id == account_id,
                        Message.deleted_at.is_(None),
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
                        Message.deleted_at.is_(None),
                        Message.search_vector.op("@@")(ts_query),
                    )
                    .order_by(desc(rank))
                    .limit(limit)
                )

            result = await session.execute(stmt)
            return list(result.scalars().all())


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
        is_spam: bool,
        source: VerdictSource,
        *,
        model_used: str | None = None,
        reasoning: str | None = None,
        neighbor_ids: list[str] | None = None,
    ) -> Verdict:
        """
        Create a new verdict for a message.

        Args:
            mail_id: Message this verdict applies to (FK column is still mail_id)
            is_spam: Spam classification result
            source: How this verdict was produced
            model_used: AI model identifier
            reasoning: Explanation text
            neighbor_ids: Vector search neighbor IDs used for context

        Returns:
            Created Verdict
        """
        verdict = Verdict(
            mail_id=mail_id,
            is_spam=is_spam,
            source=source,
            model_used=model_used,
            reasoning=reasoning,
            neighbor_ids=neighbor_ids,
        )
        async with self._db.session() as session:
            session.add(verdict)
            await session.flush()
            await session.refresh(verdict)
            return verdict

    async def get_latest_for_mail(self, mail_id: uuid.UUID) -> Verdict | None:
        """
        Get the most recent verdict for a message.

        Args:
            mail_id: Message UUID (FK column name)

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
