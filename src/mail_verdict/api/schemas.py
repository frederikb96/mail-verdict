"""
Pydantic models for REST API request/response schemas.

Provides typed serialization for all API endpoints.

PostIMAP integration: schemas align with PostIMAP-owned tables
(accounts, folders, messages, attachments) plus MailVerdict-owned
preferences tables (account_prefs, folder_prefs).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# --- Tag / Attachment schemas (referenced by MessageDetail) ---


class TagResponse(BaseModel):
    """Tag on a message."""

    tag_name: str
    source: str

    model_config = {"from_attributes": True}


class AttachmentSummary(BaseModel):
    """Attachment metadata (no data)."""

    id: uuid.UUID
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None

    model_config = {"from_attributes": True}


# --- Verdict schemas (defined before MessageDetail, which embeds one) ---


class VerdictResponse(BaseModel):
    """Verdict detail."""

    id: uuid.UUID
    message_id: uuid.UUID
    is_spam: bool
    model_used: str | None = None
    reasoning: str | None = None
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Message schemas ---


class MessageSummary(BaseModel):
    """Message list item (lightweight)."""

    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    thread_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    received_at: datetime | None = None
    is_seen: bool = False
    is_flagged: bool = False
    is_answered: bool = False
    is_draft: bool = False
    snippet: str | None = None
    pending_sync: bool = False
    is_truncated: bool = False
    thread_count: int | None = Field(
        default=None,
        description="Number of messages in the thread, only present when threaded=true",
    )
    unread_in_thread: int | None = Field(
        default=None,
        description="Unread message count in the thread, only present when threaded=true",
    )

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    """Paginated message list response with cursor-based pagination."""

    messages: list[MessageSummary]
    has_more: bool
    next_cursor: str | None = None


class MessageDetail(BaseModel):
    """Full message detail view."""

    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    thread_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    received_at: datetime | None = None
    is_seen: bool = False
    is_flagged: bool = False
    is_answered: bool = False
    is_draft: bool = False
    snippet: str | None = None
    pending_sync: bool = False
    is_truncated: bool = False
    message_id: str | None = None
    cc_addrs: Any | None = None
    bcc_addrs: Any | None = None
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: list[str] | None = None
    body_text: str | None = None
    body_html: str | None = None
    size_bytes: int | None = None
    keywords: list[str] = Field(default_factory=list)
    has_blocked_images: bool = False
    images_allowed: bool = False
    created_at: datetime
    tags: list[TagResponse] = Field(default_factory=list)
    attachments: list[AttachmentSummary] = Field(default_factory=list)
    verdict: VerdictResponse | None = None

    model_config = {"from_attributes": True}


class ThreadResponse(BaseModel):
    """Every message in a conversation, across folders, ascending by date."""

    messages: list[MessageDetail]


class MessageActionRequest(BaseModel):
    """Request to perform an action on a message."""

    action: Literal[
        "mark_read", "mark_unread", "flag", "unflag",
        "move", "archive", "trash", "expunge",
        "spam", "not_spam", "keyword_add", "keyword_remove",
    ] = Field(
        description="Action type",
    )
    target_folder_id: uuid.UUID | None = Field(
        default=None,
        description="Target folder UUID, required for the move action",
    )
    keyword: str | None = Field(
        default=None,
        description="Keyword value, required for keyword_add / keyword_remove",
    )


class MessageActionResponse(BaseModel):
    """Response from a message action."""

    success: bool
    action: str
    message_id: uuid.UUID
    message: str | None = None


# --- Bulk action schemas (client-held selection, server-side scope) ---


class BulkActionScope(BaseModel):
    """Server-resolved selection for 'select all matching' bulk actions."""

    folder_id: uuid.UUID
    filter: Literal["unread", "all"] | None = None
    exclude_ids: list[uuid.UUID] = Field(default_factory=list)


class BulkActionRequest(BaseModel):
    """Request to apply one action to many messages, by id list or by scope."""

    action: Literal[
        "move", "mark_read", "mark_unread", "flag", "unflag",
        "archive", "trash", "expunge", "spam", "not_spam",
    ]
    target_folder_id: uuid.UUID | None = Field(
        default=None,
        description="Target folder UUID, required for the move action",
    )
    ids: list[uuid.UUID] | None = None
    scope: BulkActionScope | None = None

    @model_validator(mode="after")
    def _exactly_one_of_ids_or_scope(self) -> BulkActionRequest:
        """Reject a request naming both or neither selection mechanism."""
        if (self.ids is None) == (self.scope is None):
            raise ValueError("Exactly one of 'ids' or 'scope' must be provided")
        return self

    def resolved_ids_or_scope(self) -> list[uuid.UUID] | BulkActionScope:
        """Return whichever of ids/scope was provided (validated exclusive)."""
        return self.ids if self.ids is not None else self.scope  # type: ignore[return-value]


class BulkActionResponse(BaseModel):
    """Result of a bulk action."""

    success: bool
    action: str
    affected_count: int
    errors: list[str] = Field(default_factory=list)


# --- Search schemas ---


class SearchResult(BaseModel):
    """A single search result."""

    message_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    received_at: datetime | None = None
    snippet: str | None = None

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    """Search results wrapper."""

    results: list[SearchResult]
    total: int
    query: str


# --- Account schemas ---


class SyncStatusResponse(BaseModel):
    """Sync status for an account from PostIMAP's sync_state table."""

    account_id: uuid.UUID
    state: str
    state_error: str | None = None
    last_full_sync: datetime | None = None
    last_incr_sync: datetime | None = None
    sync_tier: str | None = None
    folders_synced: int = 0
    folders_total: int = 0
    messages_synced: int = 0
    error_count: int = 0
    last_error: str | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AccountResponse(BaseModel):
    """Account summary (passwords never exposed).

    Combines PostIMAP Account fields with MailVerdict AccountPrefs.
    """

    id: uuid.UUID
    name: str
    imap_host: str
    imap_port: int
    imap_user: str
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    is_active: bool = True
    state: str = "created"
    state_error: str | None = None
    capabilities: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    # AccountPrefs fields (from account_prefs table)
    emoji: str | None = None
    spam_enabled: bool = False
    folder_order: list[str] | None = None

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    """Request to create an account."""

    name: str
    imap_host: str
    imap_port: int = 993
    imap_user: str
    imap_password: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    is_active: bool = True
    # AccountPrefs fields
    emoji: str | None = None
    spam_enabled: bool = False


class AccountUpdateRequest(BaseModel):
    """
    Request to update an account.

    imap_host/imap_port/imap_user are insert-only under PostIMAP's contract
    (its grant on `accounts` has no UPDATE on those columns) -- changing the
    IMAP host requires deleting and re-adding the account.
    """

    name: str | None = None
    imap_password: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    is_active: bool | None = None
    # AccountPrefs fields
    emoji: str | None = None
    spam_enabled: bool | None = None


# --- Folder schemas ---


class FolderResponse(BaseModel):
    """Folder summary with message counts.

    Combines PostIMAP Folder fields with MailVerdict FolderPrefs.
    """

    id: uuid.UUID
    account_id: uuid.UUID
    imap_name: str
    display_name: str | None = None
    special_use: str | None = None
    mailbox_id: str | None = None
    initial_sync_done: bool = False
    last_synced_at: datetime | None = None
    sync_error: str | None = None
    created_at: datetime | None = None
    unread_count: int = 0
    total_count: int = 0
    # FolderPrefs fields (from folder_prefs table)
    unified_name: str | None = None
    is_visible: bool = True

    model_config = {"from_attributes": True}


class FolderPrefsUpdate(BaseModel):
    """Partial update to a folder's MailVerdict preferences.

    The one write surface a folder has: visibility, display name, unified
    name, and special-use override.
    """

    is_visible: bool | None = None
    display_name: str | None = None
    unified_name: str | None = None
    special_use_override: str | None = None


class FolderCreateRequest(BaseModel):
    """Request to create a folder.

    IMAP has no parent concept -- parent_id, when given, names an existing
    folder of the same account and the full path is built by joining onto
    it with the account's separator; omitted, name is created top-level.
    """

    name: str
    parent_id: uuid.UUID | None = None


# --- Verdict schemas ---


class FeedbackRequest(BaseModel):
    """User spam feedback for a message."""

    is_spam: bool


class FeedbackResponse(BaseModel):
    """Response from spam feedback submission."""

    success: bool
    message_id: uuid.UUID
    is_spam: bool
    message: str | None = None


# --- Pipeline run schemas ---


class PipelineRunResponse(BaseModel):
    """One message's journey through the pipeline -- the "why did this
    message get that treatment" surface."""

    id: uuid.UUID
    account_id: uuid.UUID
    msg_key: str
    message_id: uuid.UUID | None
    origin: str
    apply: bool
    status: str
    skip_reason: str | None
    attempts: int
    pipeline_rev: int | None
    halted_at_stage: str | None
    failed_stage: str | None
    last_error: str | None
    trace: list[dict[str, Any]]
    model_calls: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


# --- Stats schemas ---


class WeeklyTrendPoint(BaseModel):
    """Weekly trend data point."""

    week_start: datetime
    total: int
    corrections: int
    accuracy: float


class AccountSyncStatus(BaseModel):
    """Per-account sync status."""

    account_id: uuid.UUID
    account_name: str
    last_synced_at: datetime | None = None
    folder_count: int = 0
    message_count: int = 0


class StatsResponse(BaseModel):
    """Dashboard statistics."""

    total_messages: int
    total_accounts: int
    spam_caught: int
    ham_count: int
    false_positives: int
    false_negatives: int
    fp_rate: float
    fn_rate: float
    accuracy: float
    weekly_trend: list[WeeklyTrendPoint]
    account_sync: list[AccountSyncStatus]


# --- Image exception schemas ---


class ImageExceptionCreate(BaseModel):
    """Request to create an image loading exception."""

    type: Literal["sender", "domain"]
    value: str


class ImageExceptionResponse(BaseModel):
    """Image loading exception detail."""

    id: uuid.UUID
    type: str
    value: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Folder management schemas ---


class FolderOrderItem(BaseModel):
    """Folder in ordered list with metadata."""

    folder_id: uuid.UUID
    imap_name: str
    display_name: str | None = None
    special_use: str | None = None
    is_visible: bool = True
    unread_count: int = 0
    total_count: int = 0


class FolderOrderResponse(BaseModel):
    """Ordered folder list response."""

    folders: list[FolderOrderItem]


class FolderOrderUpdate(BaseModel):
    """Request to update folder display order."""

    order: list[uuid.UUID]


# --- Unified view schemas ---


class UnifiedFolderSource(BaseModel):
    """Source folder within a unified folder grouping."""

    account_id: uuid.UUID
    account_name: str
    account_emoji: str | None
    folder_id: uuid.UUID
    imap_name: str


class UnifiedFolderResponse(BaseModel):
    """Merged folder across accounts sharing the same unified_name."""

    unified_name: str
    folders: list[UnifiedFolderSource]
    unread_count: int
    total_count: int


class UnifiedMessageSummary(BaseModel):
    """Message list item with account emoji for unified view."""

    id: uuid.UUID
    account_id: uuid.UUID
    account_emoji: str | None = None
    folder_id: uuid.UUID
    thread_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    received_at: datetime | None = None
    is_seen: bool = False
    is_flagged: bool = False
    is_answered: bool = False
    is_draft: bool = False
    snippet: str | None = None
    pending_sync: bool = False
    is_truncated: bool = False

    model_config = {"from_attributes": True}


class UnifiedMessageListResponse(BaseModel):
    """Paginated unified message list."""

    messages: list[UnifiedMessageSummary]
    has_more: bool
    next_cursor: str | None = None


class EmojiUpdate(BaseModel):
    """Request to set an account emoji."""

    emoji: str | None = Field(
        default=None,
        max_length=10,
        description="Emoji character(s) for account identification",
    )


class UnifiedFolderOrderResponse(BaseModel):
    """Unified folder display order."""

    order: list[str]


class UnifiedFolderOrderUpdate(BaseModel):
    """Request to save unified folder display order."""

    order: list[str]


# --- Outbox schemas (send / draft) ---


class OutboxCreateRequest(BaseModel):
    """Request to send a message or save a draft.

    Field names follow the UI's wire shape (to/cc/bcc), not the DB column
    names (to_addrs/cc_addrs/bcc_addrs) -- the outbox insert helper maps
    between the two.
    """

    account_id: uuid.UUID
    kind: Literal["send", "draft"]
    to: list[str] = Field(default_factory=list)
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    in_reply_to: str | None = None
    references: list[str] | None = None
    replaces_message_id: uuid.UUID | None = Field(
        default=None,
        description="The messages.id of a draft this row replaces -- editing "
        "or sending a draft leaves no duplicate behind. Requires PostIMAP "
        "service_version >= 1.4.0.",
    )


class OutboxAttachmentSummary(BaseModel):
    """Attachment metadata for an outbox row (no data)."""

    id: uuid.UUID
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None

    model_config = {"from_attributes": True}


class OutboxResponse(BaseModel):
    """Send/draft composition status."""

    id: uuid.UUID
    account_id: uuid.UUID
    kind: str
    status: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = None
    error: str | None = None
    attachments: list[OutboxAttachmentSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Queue schemas ---


class CircuitStatusResponse(BaseModel):
    """A circuit breaker's current health."""

    state: str
    reason: str | None = None
    since: datetime | None = None
    retry_after: datetime | None = None


class QueueConcurrency(BaseModel):
    """A queue's worker count: what it's set to, what's actually running, and the ceiling."""

    target: int
    actual: int
    max_allowed: int


class QueueResponse(BaseModel):
    """One named queue's full state."""

    name: str
    state: str
    concurrency: QueueConcurrency
    batch_size: int
    depth: dict[str, int]
    circuit: CircuitStatusResponse


class QueuePatchRequest(BaseModel):
    """Fields to change on a queue; omitted fields are left as-is."""

    state: Literal["running", "paused"] | None = None
    concurrency: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=1)


# --- Semantic layer schemas ---


class EmbeddingStatusResponse(BaseModel):
    """Coverage snapshot for one embedding model."""

    model: str
    in_scope: int
    encoded: int
    pending: int
    failed: int
    coverage: float


class SemanticSearchResult(BaseModel):
    """One semantic search hit: the message and how close it was."""

    message_id: uuid.UUID
    account_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    received_at: datetime | None = None
    similarity: float


class SemanticSearchResponse(BaseModel):
    """Semantic search results wrapper."""

    results: list[SemanticSearchResult]
    query: str
    model: str


# --- Pipeline configuration schemas ---


class StageOut(BaseModel):
    """One stage as stored in a pipeline revision."""

    stage_id: str
    type: str
    name: str
    config: dict[str, Any]
    enabled: bool
    halt: bool
    accounts: list[uuid.UUID] | None = None


class PipelineHealthEntryOut(BaseModel):
    """One folder reference's resolution against one account."""

    stage_id: str
    account_id: uuid.UUID
    reference: str
    ok: bool
    detail: str | None = None


class PipelineDocumentOut(BaseModel):
    """The current pipeline definition, with its live resolution state."""

    revision: int
    enabled: bool
    stages: list[StageOut]
    warnings: list[PipelineHealthEntryOut]


class PipelineWriteRequest(BaseModel):
    """Replace the whole pipeline document.

    base_revision makes the write optimistic: omit it to overwrite
    unconditionally, or supply the revision this edit was based on to get
    a 409 instead of silently clobbering a concurrent writer.
    """

    base_revision: int | None = None
    enabled: bool = True
    stages: list[dict[str, Any]] = Field(default_factory=list)


class StageCreateRequest(BaseModel):
    """Add one stage to the current pipeline definition."""

    stage_id: str
    type: str
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    halt: bool = False
    accounts: list[uuid.UUID] | None = None
    position: int | None = None  # None appends
    base_revision: int | None = None


class StageUpdateRequest(BaseModel):
    """Partial update to one existing stage; omitted fields are unchanged."""

    name: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    halt: bool | None = None
    accounts: list[uuid.UUID] | None = None
    base_revision: int | None = None


class StageReorderRequest(BaseModel):
    """The full, new stage order -- must be a permutation of every
    existing stage_id, not a partial list."""

    order: list[str]
    base_revision: int | None = None


class StageTypeOut(BaseModel):
    """One registered stage type: what it can be configured with."""

    type: str
    runs_on: list[str]
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class PipelineRevisionSummary(BaseModel):
    """One revision's metadata, without its document -- the history list."""

    revision: int
    note: str | None
    created_at: datetime


class PipelineTestRequest(BaseModel):
    """Dry-run the pipeline (or one stage) against an existing message."""

    message_id: uuid.UUID
    origin: Literal["live", "historical"] = "live"


class PipelineTestResponse(BaseModel):
    """A dry run's outcome: status and the full trace, nothing applied."""

    status: str
    skip_reason: str | None = None
    trace: list[dict[str, Any]]
