"""
SQLAlchemy ORM models for MailVerdict database.

Defines all tables: accounts, folders, mails, attachments, verdicts,
mail_tags, image_exceptions.
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


class AccountState(enum.Enum):
    """Account lifecycle states."""

    CREATED = "created"
    SYNCING = "syncing"
    SEEDING = "seeding"
    ACTIVE = "active"
    ERROR = "error"


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


class Account(Base):
    """IMAP account with encrypted credentials and lifecycle state."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False)
    imap_user: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_password: Mapped[str | None] = mapped_column(String(512), nullable=True)
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[AccountState] = mapped_column(
        Enum(AccountState, native_enum=False),
        nullable=False,
        default=AccountState.CREATED,
    )
    sync_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    embedding_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    spam_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emoji: Mapped[str | None] = mapped_column(String(10), nullable=True)
    folder_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    folder_order: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    idle_folders: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    folders: Mapped[list[Folder]] = relationship(
        "Folder",
        back_populates="account",
        cascade="all, delete-orphan",
    )
    mails: Mapped[list[Mail]] = relationship(
        "Mail",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class Folder(Base):
    """IMAP folder with sync state tracking."""

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    imap_name: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    special_use: Mapped[SpecialUse | None] = mapped_column(
        Enum(SpecialUse, native_enum=False),
        nullable=True,
    )
    separator: Mapped[str | None] = mapped_column(String(5), nullable=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uidnext: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    highestmodseq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unified_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    flags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    account: Mapped[Account] = relationship("Account", back_populates="folders")
    mails: Mapped[list[Mail]] = relationship(
        "Mail",
        back_populates="folder",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("account_id", "imap_name", name="uq_folder_account_imap_name"),
        Index("idx_folder_account_id", "account_id"),
    )


class Mail(Base):
    """Email message with full content and metadata."""

    __tablename__ = "mails"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=False,
    )
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(512), nullable=True)
    to_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cc_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    bcc_addrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_source: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modseq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dkim_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spf_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dmarc_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        nullable=True,
    )
    headers_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    body_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    account: Mapped[Account] = relationship("Account", back_populates="mails")
    folder: Mapped[Folder] = relationship("Folder", back_populates="mails")
    attachments: Mapped[list[Attachment]] = relationship(
        "Attachment",
        back_populates="mail",
        cascade="all, delete-orphan",
    )
    verdicts: Mapped[list[Verdict]] = relationship(
        "Verdict",
        back_populates="mail",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list[MailTag]] = relationship(
        "MailTag",
        back_populates="mail",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("folder_id", "uid", name="uq_mail_folder_uid"),
        Index("idx_mail_folder_uid", "folder_id", "uid"),
        Index("idx_mail_message_id", "message_id"),
        Index("idx_mail_received_at", "received_at", postgresql_using="btree"),
        Index(
            "idx_mail_folder_received",
            "folder_id",
            received_at.desc(),
            postgresql_using="btree",
        ),
        Index("idx_mail_search_vector", "search_vector", postgresql_using="gin"),
        Index("idx_mail_account_id", "account_id"),
    )


class Attachment(Base):
    """Email attachment stored as BYTEA (TOAST-compressed by Postgres)."""

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    mail_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mails.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    mail: Mapped[Mail] = relationship("Mail", back_populates="attachments")

    __table_args__ = (Index("idx_attachment_mail_id", "mail_id"),)


class Verdict(Base):
    """Spam/ham verdict for an email."""

    __tablename__ = "verdicts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    mail_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mails.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    neighbor_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    source: Mapped[VerdictSource] = mapped_column(
        Enum(VerdictSource, native_enum=False),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    mail: Mapped[Mail] = relationship("Mail", back_populates="verdicts")

    __table_args__ = (Index("idx_verdict_mail_id", "mail_id"),)


class JobState(Base):
    """Persistent state for background jobs (cursor tracking, error counts)."""

    __tablename__ = "job_states"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("name", "account_id", name="uq_job_state_name_account"),
        Index("idx_job_state_name", "name"),
    )


class MailTag(Base):
    """Tag applied to an email from various sources."""

    __tablename__ = "mail_tags"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    mail_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mails.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[TagSource] = mapped_column(
        Enum(TagSource, native_enum=False),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    mail: Mapped[Mail] = relationship("Mail", back_populates="tags")

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
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    exception_type: Mapped[ImageExceptionType] = mapped_column(
        Enum(ImageExceptionType, native_enum=False),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id", "exception_type", "value",
            name="uq_image_exception",
        ),
        Index("idx_image_exception_account", "account_id"),
    )
