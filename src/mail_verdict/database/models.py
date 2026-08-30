"""
SQLAlchemy ORM models for MailVerdict database.

PostIMAP-owned tables: accounts, folders, messages, attachments, sync_state,
  outbox, outbox_attachments, postimap_info
  (created by PostIMAP's own migrations; mapped here as a projection of the
  consumer contract -- see postimap/contract.py for the version this
  projection is built against)

MailVerdict-owned tables: verdicts, mail_tags, settings, image_exceptions,
  account_prefs, folder_prefs, queue_state, circuit_breakers, message_embeddings
  (created by Alembic, fully managed by MailVerdict)

Owned tables never carry a foreign key onto a PostIMAP-owned table: the
consumer database role has no REFERENCES grant on those tables, and
PostIMAP's retention purge must be able to delete expunged messages without
cascading away verdict history. See alembic/versions for the baseline
migration and its rationale.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    FetchedValue,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _utcnow() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


def _value_enum(enum_cls: type[enum.Enum]) -> Enum:
    """
    Build a SQLAlchemy Enum column type that persists a member's `.value`.

    SQLAlchemy's default is to persist the member's `.name` instead, which
    would silently desync from any raw SQL written against the value string
    directly (e.g. the verdicts partial unique index's `source = 'ai'`
    predicate).
    """
    return Enum(enum_cls, native_enum=False, values_callable=lambda cls: [e.value for e in cls])


# pgvector's HNSW index cannot be built above 2000 dimensions for the
# `vector` type. 1536 is text-embedding-3-small's native size; every
# embedding provider is asked to truncate to this via its API's own
# dimensions parameter, so the column never has to know which model
# produced a given row's vector -- see message_embeddings.model for that.
EMBEDDING_DIMENSIONS = 1536

# --- Enums ---


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
    """IMAP/SMTP account -- PostIMAP-owned table.

    imap_password/smtp_password are consumer-writable but only ever in the
    contract's plaintext format (a 0x00 prefix byte); PostIMAP re-encrypts
    them itself. See postimap/actions.py for the write helpers.
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
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
    # PostIMAP owns the lifecycle state; see the note on Outbox.status for why
    # a Python-side default here would be sent on INSERT and accepted.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=FetchedValue(),
    )
    state_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Folder(Base):
    """IMAP folder -- PostIMAP-owned table.

    account_id and imap_name are insert-only (see postimap/actions.py's
    create_folder() -- id carries no INSERT grant on this table, so that
    helper issues a Core INSERT naming only the granted columns and reads
    the id back via RETURNING rather than letting an ORM-constructed row
    send its own client-side id). deleted_at is the one UPDATE surface,
    for deletion. Everything else is PostIMAP's own sync bookkeeping.
    """

    __tablename__ = "folders"

    # id/total_count/unread_count/initial_sync_done carry no INSERT grant on
    # this table -- server_default=FetchedValue() keeps a value here for
    # ORM convenience without ever sending it explicitly, the same guard
    # next_retry_at uses below on Outbox.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    imap_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    separator: Mapped[str | None] = mapped_column(String(1), nullable=True)
    mailbox_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    special_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uidnext: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    highestmodseq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=FetchedValue(),
    )
    unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=FetchedValue(),
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    initial_sync_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Message(Base):
    """Mirrored email message -- PostIMAP-owned table.

    There is no INSERT grant on this table: a message row exists because it
    exists on the IMAP server, and only PostIMAP's own sync engine creates
    one. MailVerdict never originates mail by inserting into messages --
    to send or draft, insert into outbox instead; the copy appears here
    once it syncs back.

    imap_uid is nullable: NULL means an optimistic folder move is pending
    (surfaced in the API as pending_sync). expunged_at is a soft-delete
    tombstone, distinct from the is_deleted \\Deleted flag -- see
    postimap/actions.py for the move/expunge helpers that are the only
    place these columns are written from MailVerdict.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    folder_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    imap_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    cc_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    bcc_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    msg_references: Mapped[list[str] | None] = mapped_column(
        "references", ARRAY(Text), nullable=True,
    )
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_source: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    expunged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_messages_folder_uid", "folder_id", "imap_uid"),
        Index("idx_messages_message_id", "message_id"),
        Index(
            "idx_messages_folder_received",
            "folder_id",
            received_at.desc(),
            postgresql_using="btree",
        ),
        Index("idx_messages_search_vector", "search_vector", postgresql_using="gin"),
        Index("idx_messages_account_id", "account_id"),
        Index("idx_messages_thread_id", "account_id", "thread_id"),
    )


class SyncState(Base):
    """Per-account sync health -- PostIMAP-owned table, read-only."""

    __tablename__ = "sync_state"

    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    last_full_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_incr_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sync_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    folders_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    folders_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class Attachment(Base):
    """Email attachment -- PostIMAP-owned table, read-only."""

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    __table_args__ = (Index("idx_attachments_message_id", "message_id"),)


class Outbox(Base):
    """Send/draft composition queue -- PostIMAP-owned table.

    account_id/kind/from_addr/to_addrs/cc_addrs/bcc_addrs/subject/body_text/
    body_html/in_reply_to/references/max_attempts/replaces_message_id are
    insert-only from the consumer side; everything else (status, error,
    attempts, sent_message_id, sent_at) is PostIMAP-managed. See
    postimap/actions.py for the insert helper and the contract's outbox
    worked examples.
    """

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    cc_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    bcc_addrs: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    msg_references: Mapped[list[str] | None] = mapped_column(
        "references", ARRAY(Text), nullable=True,
    )
    # The message this row supersedes -- see postimap/actions.py's
    # insert_outbox(). References messages(id) with ON DELETE SET NULL on
    # PostIMAP's side; this projection carries no FK of its own, consistent
    # with every other column here.
    replaces_message_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # server_default=FetchedValue() tells SQLAlchemy this column is entirely
    # PostIMAP-managed: never send it explicitly on INSERT (the real column
    # is NOT NULL with its own server-side default) and never try to fetch
    # it back automatically -- this projection has no DDL of its own for a
    # table Alembic doesn't create.
    #
    # A Python-side default would be sent on every INSERT, which matters more
    # than it looks: the table carries a table-level INSERT grant, so a value
    # written here is accepted rather than refused. A status PostIMAP does not
    # treat as claimable means the row is never picked up and the mail never
    # goes out, with nothing reporting an error.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=FetchedValue(),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=FetchedValue(),
    )
    # Insertable by the consumer, per the contract -- unlike the two above.
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=FetchedValue(),
    )
    sent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (Index("idx_outbox_account_id", "account_id"),)


class OutboxAttachment(Base):
    """Attachment for an outbox row -- PostIMAP-owned table, insert/select only."""

    __tablename__ = "outbox_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    outbox_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    __table_args__ = (Index("idx_outbox_attachments_outbox_id", "outbox_id"),)


class PostimapInfo(Base):
    """Single-row contract version/service version handshake, read-only."""

    __tablename__ = "postimap_info"

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    service_version: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


# --- MailVerdict-owned tables (created by Alembic) ---
#
# None of these carry a ForeignKey onto a PostIMAP-owned table: the
# postimap_app role grants SELECT/INSERT/UPDATE only, never REFERENCES, and
# retention purge of expunged messages must not cascade-delete verdict
# history. Every *_id column below is a plain UUID column, joined explicitly
# in queries rather than via an ORM relationship.


class AccountPrefs(Base):
    """Per-account MailVerdict preferences (not in PostIMAP)."""

    __tablename__ = "account_prefs"

    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    emoji: Mapped[str | None] = mapped_column(String(10), nullable=True)
    spam_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    folder_order: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)


class FolderPrefs(Base):
    """Per-folder MailVerdict UI preferences (not in PostIMAP)."""

    __tablename__ = "folder_prefs"

    folder_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unified_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    special_use_override: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class ProviderCredential(Base):
    """Encrypted AI provider API key, one row per provider.

    encrypted_key is AES-256-GCM ciphertext (core/encryption.py) -- never
    plaintext at rest, and never read back out through the API. Settings
    writes go through settings/credentials.py, the only code that decrypts
    a row.
    """

    __tablename__ = "provider_credentials"

    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class Verdict(Base):
    """Spam/ham verdict for an email.

    msg_key is the durable identity (see database/msg_key.py): the
    Message-ID header when present, otherwise a content hash. The partial
    unique index on (account_id, msg_key, coalesce(from_addr, '')) for AI
    verdicts is the never-reclassify gate: it survives PostIMAP's retention
    purge (which deletes the message row but not this one) and a
    UIDVALIDITY resync (which assigns a new message UUID but keeps the same
    key). from_addr is included so that a message forging the Message-ID of
    one already verdicted cannot bypass classification. "Current verdict"
    for a mail_id is the latest row by created_at -- user_feedback inserts
    additional rows rather than overwriting.
    """

    __tablename__ = "verdicts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    mail_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    message_id_hdr: Mapped[str | None] = mapped_column(Text, nullable=True)
    msg_key: Mapped[str] = mapped_column(Text, nullable=False)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[VerdictSource] = mapped_column(
        _value_enum(VerdictSource), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_verdict_mail_id", "mail_id"),
        Index("idx_verdict_account_id", "account_id"),
    )


class MailTag(Base):
    """Tag applied to an email from various sources."""

    __tablename__ = "mail_tags"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    mail_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tag_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[TagSource] = mapped_column(
        _value_enum(TagSource), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("mail_id", "tag_name", name="uq_mail_tag"),
        Index("idx_mail_tag_mail_id", "mail_id"),
    )


class ImageException(Base):
    """Per-account exception for remote image loading (sender or domain allowlist)."""

    __tablename__ = "image_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    exception_type: Mapped[ImageExceptionType] = mapped_column(
        _value_enum(ImageExceptionType), nullable=False,
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


class QueueState(Base):
    """Operator-controlled lifecycle for one named work queue (queue/manager.py).

    Concurrency and pause/resume are changed through the queue API and take
    effect on the running supervisor immediately; this row is what makes
    that survive a restart rather than resetting to a hardcoded default.
    Domain-agnostic on purpose -- see queue/work_queue.py's module
    docstring for why this table has no knowledge of what it queues.
    """

    __tablename__ = "queue_state"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class CircuitBreakerState(Base):
    """Named health gate (queue/circuit.py), independent of any one queue --
    a provider's circuit breaker can be shared across every queue that
    calls it, which is the whole point of keying it by an arbitrary name
    rather than by queue.
    """

    __tablename__ = "circuit_breakers"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="closed")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )


class MessageEmbedding(Base):
    """One vector per (account_id, msg_key, model) -- the semantic search
    corpus and, via its status/attempts/lease columns, the work queue that
    fills it (queue/work_queue.py).

    Keyed on msg_key rather than message_id: a UIDVALIDITY resync replaces
    every messages.id in a folder, which would silently orphan a row keyed
    on it. message_id is kept only as a join hint, re-resolved at read
    time. model is part of the identity rather than a separate "current
    model" column, so a re-encode after a model change is an ordinary
    insert under a new key rather than an update racing the old rows --
    coverage for the new model starts at zero and rises, it never mixes
    two vector spaces under one key.
    """

    __tablename__ = "message_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    msg_key: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    content_level: Mapped[str] = mapped_column(Text, nullable=False, default="full")
    source_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id", "msg_key", "model", name="uq_message_embeddings_account_msgkey_model",
        ),
        Index("ix_message_embeddings_account", "account_id", "model"),
    )


class PipelineRun(Base):
    """One message's journey through the pipeline -- also the queue row
    claimed by queue/work_queue.py's generic engine, and the durable
    record that the journey happened at all.

    `dedup_key` is what makes "exactly one live run per message, ever"
    hold: live mail always dedups to the literal string 'live', so a
    second insert attempt for the same (account_id, msg_key) is absorbed
    by `uq_pipeline_run` rather than producing a duplicate journey. A
    sweep (not built by this revision) would dedup to its own id instead.
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    msg_key: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    pipeline_rev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    halted_at_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
        onupdate=_utcnow, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("account_id", "msg_key", "dedup_key", name="uq_pipeline_run"),
        Index("idx_pipeline_run_claim", "priority", "next_attempt_at"),
        Index("idx_pipeline_run_lease", "lease_expires_at"),
        Index("idx_pipeline_run_failed", "account_id", "finished_at"),
        Index("idx_pipeline_run_sweep", "sweep_id"),
    )


class PipelineRevision(Base):
    """One revision of the pipeline definition -- append-only, current
    definition is `max(revision)`. See pipeline/revisions.py.
    """

    __tablename__ = "pipeline_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class PipelineFolderState(Base):
    """MailVerdict's own watermark, per folder: when its first full sync
    completed. `folders.initial_sync_done` is a boolean with no timestamp,
    which is why this needs to be a column of our own -- reconciliation
    has to tell "arrived while disconnected" (must be enqueued) from
    "historical" (must never be), and only a timestamp can do that. See
    pipeline/enqueue.py.
    """

    __tablename__ = "pipeline_folder_state"

    folder_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    backfill_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
