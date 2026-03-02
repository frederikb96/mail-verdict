"""
Repository layer for database operations.

All queries are account_id scoped for multi-account isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.database.models import (
    Attachment,
    Folder,
    Mail,
    MailTag,
    TagSource,
    Verdict,
    VerdictSource,
)

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection


class MailRepository:
    """
    Repository for Mail CRUD operations.

    All queries are scoped by account_id.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def upsert_mail(
        self,
        account_id: uuid.UUID,
        folder_id: uuid.UUID,
        uid: int,
        *,
        message_id: str | None = None,
        subject: str | None = None,
        from_addr: str | None = None,
        to_addrs: dict[str, Any] | None = None,
        cc_addrs: dict[str, Any] | None = None,
        bcc_addrs: dict[str, Any] | None = None,
        body_text: str | None = None,
        body_html: str | None = None,
        raw_headers: dict[str, Any] | None = None,
        raw_source: bytes | None = None,
        received_at: datetime | None = None,
        size_bytes: int | None = None,
        modseq: int | None = None,
        is_read: bool = False,
        is_flagged: bool = False,
        is_deleted: bool = False,
        dkim_pass: bool | None = None,
        spf_pass: bool | None = None,
        dmarc_pass: bool | None = None,
    ) -> Mail:
        """
        Insert or update a mail by (folder_id, uid) uniqueness.

        On conflict, updates mutable fields (flags, body, headers).

        Args:
            account_id: Owning account
            folder_id: IMAP folder
            uid: IMAP UID within folder
            **kwargs: Mail field values

        Returns:
            The upserted Mail object
        """
        values: dict[str, Any] = {
            "account_id": account_id,
            "folder_id": folder_id,
            "uid": uid,
            "message_id": message_id,
            "subject": subject,
            "from_addr": from_addr,
            "to_addrs": to_addrs,
            "cc_addrs": cc_addrs,
            "bcc_addrs": bcc_addrs,
            "body_text": body_text,
            "body_html": body_html,
            "raw_headers": raw_headers,
            "raw_source": raw_source,
            "received_at": received_at,
            "size_bytes": size_bytes,
            "modseq": modseq,
            "is_read": is_read,
            "is_flagged": is_flagged,
            "is_deleted": is_deleted,
            "dkim_pass": dkim_pass,
            "spf_pass": spf_pass,
            "dmarc_pass": dmarc_pass,
        }

        # On conflict, only update fields that were explicitly provided.
        # Boolean flags (is_read, is_flagged, is_deleted) always update.
        # Other fields only update when non-None to avoid overwriting
        # existing data during flag-only updates.
        update_cols: dict[str, Any] = {
            "is_read": is_read,
            "is_flagged": is_flagged,
            "is_deleted": is_deleted,
        }
        optional_updates = {
            "subject": subject,
            "from_addr": from_addr,
            "to_addrs": to_addrs,
            "cc_addrs": cc_addrs,
            "bcc_addrs": bcc_addrs,
            "body_text": body_text,
            "body_html": body_html,
            "raw_headers": raw_headers,
            "raw_source": raw_source,
            "size_bytes": size_bytes,
            "modseq": modseq,
            "dkim_pass": dkim_pass,
            "spf_pass": spf_pass,
            "dmarc_pass": dmarc_pass,
        }
        for key, val in optional_updates.items():
            if val is not None:
                update_cols[key] = val

        async with self._db.session() as session:
            stmt = (
                pg_insert(Mail)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_mail_folder_uid",
                    set_=update_cols,
                )
                .returning(Mail)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_by_message_id(
        self,
        account_id: uuid.UUID,
        message_id: str,
    ) -> list[Mail]:
        """
        Find mails by RFC 2822 Message-ID.

        Args:
            account_id: Account scope
            message_id: RFC 2822 Message-ID header value

        Returns:
            Matching mails (may span folders)
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Mail).where(
                    Mail.account_id == account_id,
                    Mail.message_id == message_id,
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
    ) -> list[Mail]:
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
            Mails ranked by relevance
        """
        async with self._db.session() as session:
            ts_query = func.plainto_tsquery("english", query)

            if fuzzy:
                # Combined: tsvector rank + trigram similarity
                rank = func.ts_rank(Mail.search_vector, ts_query)
                trgm_sim = func.similarity(Mail.subject, query)
                stmt = (
                    select(Mail)
                    .where(
                        Mail.account_id == account_id,
                        (Mail.search_vector.op("@@")(ts_query))
                        | (func.similarity(Mail.subject, query) >= similarity_threshold)
                        | (func.similarity(Mail.body_text, query) >= similarity_threshold),
                    )
                    .order_by(desc(rank + trgm_sim))
                    .limit(limit)
                )
            else:
                rank = func.ts_rank(Mail.search_vector, ts_query)
                stmt = (
                    select(Mail)
                    .where(
                        Mail.account_id == account_id,
                        Mail.search_vector.op("@@")(ts_query),
                    )
                    .order_by(desc(rank))
                    .limit(limit)
                )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_unprocessed(
        self,
        account_id: uuid.UUID,
        *,
        limit: int = 100,
    ) -> list[Mail]:
        """
        Get mails without any verdict.

        Args:
            account_id: Account scope
            limit: Max results

        Returns:
            Mails that have no associated verdict
        """
        async with self._db.session() as session:
            subq = select(Verdict.mail_id).distinct()
            stmt = (
                select(Mail)
                .where(
                    Mail.account_id == account_id,
                    ~Mail.id.in_(subq),
                )
                .order_by(desc(Mail.received_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_uids_by_folder(self, folder_id: uuid.UUID) -> set[int]:
        """
        Get all UIDs for a folder (lightweight, no full ORM load).

        Args:
            folder_id: Folder UUID

        Returns:
            Set of IMAP UIDs in the folder
        """
        async with self._db.session() as session:
            result = await session.execute(select(Mail.uid).where(Mail.folder_id == folder_id))
            return {row[0] for row in result.all()}

    async def get_by_folder_and_uid(
        self,
        folder_id: uuid.UUID,
        uid: int,
    ) -> Mail | None:
        """
        Get a single mail by folder and IMAP UID.

        Args:
            folder_id: Folder UUID
            uid: IMAP UID within folder

        Returns:
            Mail if found, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Mail).where(
                    Mail.folder_id == folder_id,
                    Mail.uid == uid,
                )
            )
            return result.scalar_one_or_none()

    async def get_by_folder(
        self,
        folder_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Mail]:
        """
        Get mails in a folder, newest first.

        Args:
            folder_id: Folder to list
            limit: Max results
            offset: Skip count

        Returns:
            Mails in the folder
        """
        async with self._db.session() as session:
            stmt = (
                select(Mail)
                .where(Mail.folder_id == folder_id)
                .order_by(desc(Mail.received_at))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_by_id(
        self,
        account_id: uuid.UUID,
        mail_id: uuid.UUID,
    ) -> Mail | None:
        """
        Get a single mail by ID with account scoping.

        Args:
            account_id: Account scope
            mail_id: Mail UUID

        Returns:
            Mail if found and owned by account, None otherwise
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Mail).where(
                    Mail.id == mail_id,
                    Mail.account_id == account_id,
                )
            )
            return result.scalar_one_or_none()


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
        Create a new verdict for a mail.

        Args:
            mail_id: Mail this verdict applies to
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
        Get the most recent verdict for a mail.

        Args:
            mail_id: Mail UUID

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
            account_id: Account scope (joins through Mail)

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
                .join(Mail, Verdict.mail_id == Mail.id)
                .where(Mail.account_id == account_id)
            )
            result = await session.execute(stmt)
            row = result.one()
            return {
                "total": row.total,
                "spam": row.spam,
                "ham": row.ham,
            }


class FolderRepository:
    """
    Repository for Folder CRUD and sync state operations.

    Combines folder management with sync state tracking.
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def upsert_folder(
        self,
        account_id: uuid.UUID,
        imap_name: str,
        *,
        display_name: str | None = None,
        special_use: str | None = None,
        separator: str | None = None,
        subscribed: bool = True,
        flags: list[str] | None = None,
    ) -> Folder:
        """
        Insert or update a folder by (account_id, imap_name) uniqueness.

        Args:
            account_id: Owning account
            imap_name: Raw IMAP folder path
            display_name: Human-readable name
            special_use: RFC 6154 special-use attribute
            separator: IMAP hierarchy separator
            subscribed: Whether folder is subscribed
            flags: IMAP folder flags

        Returns:
            The upserted Folder
        """
        from mail_verdict.database.models import SpecialUse

        special_use_enum = SpecialUse(special_use) if special_use else None

        values: dict[str, Any] = {
            "account_id": account_id,
            "imap_name": imap_name,
            "display_name": display_name,
            "special_use": special_use_enum,
            "separator": separator,
            "subscribed": subscribed,
            "flags": flags,
        }

        update_cols = {
            "display_name": display_name,
            "special_use": special_use_enum,
            "separator": separator,
            "subscribed": subscribed,
            "flags": flags,
        }

        async with self._db.session() as session:
            stmt = (
                pg_insert(Folder)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_folder_account_imap_name",
                    set_=update_cols,
                )
                .returning(Folder)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_by_account(self, account_id: uuid.UUID) -> list[Folder]:
        """
        Get all folders for an account.

        Args:
            account_id: Account UUID

        Returns:
            List of Folder objects
        """
        async with self._db.session() as session:
            result = await session.execute(select(Folder).where(Folder.account_id == account_id))
            return list(result.scalars().all())

    async def get_state(
        self,
        account_id: uuid.UUID,
        imap_name: str,
    ) -> Folder | None:
        """
        Get sync state for a specific folder.

        Args:
            account_id: Account scope
            imap_name: IMAP folder path

        Returns:
            Folder with sync state fields, or None
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Folder).where(
                    Folder.account_id == account_id,
                    Folder.imap_name == imap_name,
                )
            )
            return result.scalar_one_or_none()

    async def update_state(
        self,
        folder_id: uuid.UUID,
        *,
        uidvalidity: int | None = None,
        uidnext: int | None = None,
        highestmodseq: int | None = None,
        last_synced_at: datetime | None = None,
    ) -> bool:
        """
        Update sync state fields for a folder.

        Args:
            folder_id: Folder to update
            uidvalidity: IMAP UIDVALIDITY
            uidnext: IMAP UIDNEXT
            highestmodseq: CONDSTORE HIGHESTMODSEQ
            last_synced_at: Timestamp of last successful sync

        Returns:
            True if folder was updated
        """
        values: dict[str, Any] = {}
        if uidvalidity is not None:
            values["uidvalidity"] = uidvalidity
        if uidnext is not None:
            values["uidnext"] = uidnext
        if highestmodseq is not None:
            values["highestmodseq"] = highestmodseq
        if last_synced_at is not None:
            values["last_synced_at"] = last_synced_at
        else:
            values["last_synced_at"] = datetime.now(timezone.utc)

        if not values:
            return True

        async with self._db.session() as session:
            stmt = update(Folder).where(Folder.id == folder_id).values(**values)
            result = await session.execute(stmt)
            return bool(result.rowcount > 0)  # type: ignore[attr-defined]


class AttachmentRepository:
    """Repository for Attachment CRUD operations."""

    def __init__(self, db: DatabaseConnection) -> None:
        """
        Initialize repository with database connection.

        Args:
            db: Database connection instance
        """
        self._db = db

    async def create(
        self,
        mail_id: uuid.UUID,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        content_id: str | None = None,
        size_bytes: int | None = None,
        data: bytes | None = None,
    ) -> Attachment:
        """
        Store an attachment.

        Args:
            mail_id: Parent mail
            filename: Original filename
            content_type: MIME type
            content_id: CID for inline attachments
            size_bytes: Size in bytes
            data: Raw attachment bytes (TOAST-compressed by Postgres)

        Returns:
            Created Attachment
        """
        attachment = Attachment(
            mail_id=mail_id,
            filename=filename,
            content_type=content_type,
            content_id=content_id,
            size_bytes=size_bytes,
            data=data,
        )
        async with self._db.session() as session:
            session.add(attachment)
            await session.flush()
            await session.refresh(attachment)
            return attachment

    async def get_by_mail_id(self, mail_id: uuid.UUID) -> list[Attachment]:
        """
        Get all attachments for a mail.

        Args:
            mail_id: Parent mail UUID

        Returns:
            List of Attachment objects
        """
        async with self._db.session() as session:
            result = await session.execute(select(Attachment).where(Attachment.mail_id == mail_id))
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
        Add a tag to a mail (idempotent via upsert).

        Args:
            mail_id: Mail to tag
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
        Remove a tag from a mail.

        Args:
            mail_id: Mail UUID
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
        Get all tags for a mail.

        Args:
            mail_id: Mail UUID

        Returns:
            List of MailTag objects
        """
        async with self._db.session() as session:
            result = await session.execute(select(MailTag).where(MailTag.mail_id == mail_id))
            return list(result.scalars().all())
