"""
SQLAlchemy ORM models for MailVerdict database.

PostIMAP-owned tables: accounts, folders, messages, attachments
  (created by PostIMAP's Kysely migrations, mapped here read/write)

MailVerdict-owned tables: verdicts, mail_tags, settings, image_exceptions,
  account_prefs, folder_prefs
  (created by Alembic, fully managed by MailVerdict)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _utcnow() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


# --- Enums ---

class SpecialUse(enum.Enum):
    """IMAP special-use folder types (RFC 6154)."""

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    ALL = "all"
    FLAGGED = "flagged"


class AccountState(enum.Enum):
    """Account lifecycle states (matches PostIMAP CHECK constraint)."""

    CREATED = "created"
    SYNCING = "syncing"
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class VerdictSource(enum.Enum):
    """Source of a verdict decision."""

    AI = "ai"
    RULE = "rule"
    USER_FEEDBACK = "user_feedback"


class TagSource(enum.Enum):
    """Source of a mail tag."""

    ENRICHMENT = "enrichment"
    RULE = "rule"
    USER = "user"
    SPAM = "spam"
    IMAP = "imap"


class ImageExceptionType(enum.Enum):
    """Type of image loading exception."""

    SENDER = "sender"
    DOMAIN = "domain"


# --- PostIMAP-owned tables (mapped, not created by Alembic) ---

class Account(Base):
    """IMAP account — PostIMAP-owned table."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    imap_host: Mapped[str] = mapped_column(Text, nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    imap_user: Mapped[str] = mapped_column(Text, nullable=False)
    imap_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    smtp_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_password: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="created")
    state_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    folders: Mapped[list[Folder]] = relationship(
        "Folder", back_populates="account", passive_deletes=True,
    )
    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="account", passive_deletes=True,
    )
    prefs: Mapped[AccountPrefs | None] = relationship(
        "AccountPrefs", back_populates="account", uselist=False,
        cascade="all, delete-orphan",
    )


class Folder(Base):
    """IMAP folder — PostIMAP-owned table."""

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
    )
    imap_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    separator: Mapped[str | None] = mapped_column(String(1), nullable=True)
    mailbox_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    special_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uidnext: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    highestmodseq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exists_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    account: Mapped[Account] = relationship("Account", back_populates="folders")
    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="folder", passive_deletes=True,
    )
    prefs: Mapped[FolderPrefs | None] = relationship(
        "FolderPrefs", back_populates="folder", uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("account_id", "imap_name", name="folders_account_id_imap_name_unique"),
        Index("idx_folders_account_id", "account_id"),
    )


class Message(Base):
    """Email message — PostIMAP-owned table (was 'mails').

    sync_version is intentionally NOT mapped to prevent accidental writes.
    PostIMAP uses sync_version for loop prevention — MailVerdict must never touch it.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=False,
    )
    imap_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cc_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    bcc_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    msg_references: Mapped[list[str] | None] = mapped_column(
        "references", ARRAY(Text), nullable=True,
    )
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_source: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modseq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_answered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    account: Mapped[Account] = relationship("Account", back_populates="messages")
    folder: Mapped[Folder] = relationship("Folder", back_populates="messages")
    attachments: Mapped[list[Attachment]] = relationship(
        "Attachment", back_populates="message", passive_deletes=True,
    )
    verdicts: Mapped[list[Verdict]] = relationship(
        "Verdict", back_populates="message", cascade="all, delete-orphan",
    )
    tags: Mapped[list[MailTag]] = relationship(
        "MailTag", back_populates="message", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("folder_id", "imap_uid", name="messages_folder_id_imap_uid_unique"),
        Index("idx_messages_folder_uid", "folder_id", "imap_uid"),
        Index("idx_messages_message_id", "message_id"),
        Index("idx_messages_received_at", "received_at", postgresql_using="btree"),
        Index(
            "idx_messages_folder_received",
            "folder_id",
            received_at.desc(),
            postgresql_using="btree",
        ),
        Index("idx_messages_search_vector", "search_vector", postgresql_using="gin"),
        Index("idx_messages_account_id", "account_id"),
    )


class Attachment(Base):
    """Email attachment — PostIMAP-owned table."""

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    message: Mapped[Message] = relationship("Message", back_populates="attachments")

    __table_args__ = (Index("idx_attachments_message_id", "message_id"),)


# --- MailVerdict-owned tables (created by Alembic) ---

class AccountPrefs(Base):
    """Per-account MailVerdict preferences (not in PostIMAP)."""

    __tablename__ = "account_prefs"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True,
    )
    emoji: Mapped[str | None] = mapped_column(String(10), nullable=True)
    spam_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    embedding_lookback_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30,
    )
    folder_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    folder_order: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    account: Mapped[Account] = relationship("Account", back_populates="prefs")


class FolderPrefs(Base):
    """Per-folder MailVerdict UI preferences (not in PostIMAP)."""

    __tablename__ = "folder_prefs"

    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True,
    )
    unified_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    folder: Mapped[Folder] = relationship("Folder", back_populates="prefs")


class Setting(Base):
    """Application setting stored as JSONB by category."""

    __tablename__ = "settings"

    category: Mapped[str] = mapped_column(String(100), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class Verdict(Base):
    """Spam/ham verdict for an email."""

    __tablename__ = "verdicts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    mail_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    neighbor_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    source: Mapped[VerdictSource] = mapped_column(
        Enum(VerdictSource, native_enum=False), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )

    message: Mapped[Message] = relationship("Message", back_populates="verdicts")

    __table_args__ = (Index("idx_verdict_mail_id", "mail_id"),)


class MailTag(Base):
    """Tag applied to an email from various sources."""

    __tablename__ = "mail_tags"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    mail_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    tag_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[TagSource] = mapped_column(
        Enum(TagSource, native_enum=False), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )

    message: Mapped[Message] = relationship("Message", back_populates="tags")

    __table_args__ = (
        UniqueConstraint("mail_id", "tag_name", name="uq_mail_tag"),
        Index("idx_mail_tag_mail_id", "mail_id"),
    )


class ImageException(Base):
    """Per-account exception for remote image loading (sender or domain allowlist)."""

    __tablename__ = "image_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
    )
    exception_type: Mapped[ImageExceptionType] = mapped_column(
        Enum(ImageExceptionType, native_enum=False), nullable=False,
    )
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id", "exception_type", "value", name="uq_image_exception",
        ),
        Index("idx_image_exception_account", "account_id"),
    )
