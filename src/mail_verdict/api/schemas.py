"""
Pydantic models for REST API request/response schemas.

Provides typed serialization for all API endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Tag / Attachment schemas (referenced by MailDetail) ---


class TagResponse(BaseModel):
    """Tag on a mail."""

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


# --- Mail schemas ---


class MailSummary(BaseModel):
    """Mail list item (lightweight)."""

    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    received_at: datetime | None = None
    is_read: bool = False
    is_flagged: bool = False
    is_deleted: bool = False
    headers_synced: bool = False
    body_synced: bool = False
    snippet: str | None = None

    model_config = {"from_attributes": True}


class MailListResponse(BaseModel):
    """Paginated mail list response with cursor-based pagination."""

    mails: list[MailSummary]
    has_more: bool
    next_cursor: str | None = None


class MailDetail(BaseModel):
    """Full mail detail view."""

    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    uid: int
    message_id: str | None = None
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    cc_addrs: Any | None = None
    bcc_addrs: Any | None = None
    body_text: str | None = None
    body_html: str | None = None
    raw_headers: dict[str, Any] | None = None
    received_at: datetime | None = None
    size_bytes: int | None = None
    is_read: bool = False
    is_flagged: bool = False
    is_deleted: bool = False
    dkim_pass: bool | None = None
    spf_pass: bool | None = None
    dmarc_pass: bool | None = None
    headers_synced: bool = False
    body_synced: bool = False
    fetched_at: datetime
    created_at: datetime
    has_blocked_images: bool = False
    images_allowed: bool = False
    tags: list[TagResponse] = Field(default_factory=list)
    attachments: list[AttachmentSummary] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MailActionRequest(BaseModel):
    """Request to perform an action on a mail."""

    action: Literal[
        "move", "mark_read", "mark_unread", "delete",
        "flag", "unflag", "archive", "spam",
    ] = Field(
        description="Action type",
    )
    target_folder: str | None = Field(
        default=None,
        description="Target folder for move action",
    )


class MailActionResponse(BaseModel):
    """Response from a mail action."""

    success: bool
    action: str
    mail_id: uuid.UUID
    message: str | None = None


# --- Search schemas ---


class SearchResult(BaseModel):
    """A single search result."""

    mail_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    received_at: datetime | None = None
    score: float = 0.0
    source: str = "fulltext"

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    """Search results wrapper."""

    results: list[SearchResult]
    total: int
    mode: str
    query: str


# --- Account schemas ---


class AccountResponse(BaseModel):
    """Account summary (passwords never exposed)."""

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
    emoji: str | None = None
    sync_lookback_days: int = 180
    embedding_lookback_days: int = 30
    spam_enabled: bool = False
    folder_mapping: dict[str, str | None] | None = None
    created_at: datetime

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
    sync_lookback_days: int = 180
    embedding_lookback_days: int = 30
    spam_enabled: bool = False


class AccountUpdateRequest(BaseModel):
    """Request to update an account."""

    name: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_user: str | None = None
    imap_password: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    is_active: bool | None = None
    sync_lookback_days: int | None = None
    embedding_lookback_days: int | None = None
    spam_enabled: bool | None = None


# --- Folder schemas ---


class FolderResponse(BaseModel):
    """Folder summary with message counts."""

    id: uuid.UUID
    account_id: uuid.UUID
    imap_name: str
    display_name: str | None = None
    special_use: str | None = None
    unified_name: str | None = None
    subscribed: bool = True
    is_visible: bool = True
    last_synced_at: datetime | None = None
    unread_count: int = 0
    total_count: int = 0

    model_config = {"from_attributes": True}


# --- Verdict schemas ---


class VerdictResponse(BaseModel):
    """Verdict detail."""

    id: uuid.UUID
    mail_id: uuid.UUID
    is_spam: bool
    model_used: str | None = None
    reasoning: str | None = None
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackRequest(BaseModel):
    """User spam feedback for a mail."""

    is_spam: bool


class FeedbackResponse(BaseModel):
    """Response from spam feedback submission."""

    success: bool
    mail_id: uuid.UUID
    is_spam: bool
    message: str | None = None


# --- Rule schemas ---


class RuleResponse(BaseModel):
    """Rule from config."""

    index: int
    name: str
    trigger: str
    conditions: dict[str, Any] | list[Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    enrichment: dict[str, Any] = Field(default_factory=dict)


class RuleTestRequest(BaseModel):
    """Request to test a rule against a mail."""

    mail_id: uuid.UUID
    account_id: uuid.UUID


class RuleTestResponse(BaseModel):
    """Result of a rule dry-run test."""

    rule_name: str
    conditions_matched: bool
    actions_would_run: list[dict[str, Any]]


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
    mail_count: int = 0


class StatsResponse(BaseModel):
    """Dashboard statistics."""

    total_mails: int
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


class FolderVisibilityUpdate(BaseModel):
    """Request to toggle folder visibility."""

    is_visible: bool


class FolderVisibilityResponse(BaseModel):
    """Folder visibility update response."""

    folder_id: uuid.UUID
    is_visible: bool


# --- IDLE configuration schemas ---


class IdleFolderItem(BaseModel):
    """Folder with IDLE status."""

    folder_id: uuid.UUID
    imap_name: str
    idle_enabled: bool
    idle_supported: bool | None = None


class IdleFolderToggle(BaseModel):
    """Request to toggle IDLE for a folder."""

    folder_id: uuid.UUID
    enabled: bool


class IdleFolderToggleResponse(BaseModel):
    """IDLE toggle response."""

    folder_id: uuid.UUID
    enabled: bool
    success: bool
    error: str | None = None


class IdleValidationRequest(BaseModel):
    """Request to validate IDLE support for a folder."""

    folder_id: uuid.UUID


class IdleValidationResponse(BaseModel):
    """IDLE validation result."""

    folder_id: uuid.UUID
    supported: bool
    error: str | None = None


# --- Selection / bulk action schemas ---


class SelectionResponse(BaseModel):
    """Current selection state for an account."""

    selected_ids: list[uuid.UUID]
    count: int


class SelectionToggle(BaseModel):
    """Request to toggle a single mail's selection."""

    mail_id: uuid.UUID


class SelectionRange(BaseModel):
    """Request for shift-click range selection."""

    from_id: uuid.UUID
    to_id: uuid.UUID
    folder_id: uuid.UUID


class SelectionAll(BaseModel):
    """Request to select all mails in a folder."""

    folder_id: uuid.UUID


class BulkActionRequest(BaseModel):
    """Request to execute an action on all selected mails."""

    action: Literal[
        "move", "archive", "spam", "star", "unstar",
        "mark_read", "mark_unread", "delete",
    ]
    target_folder_id: uuid.UUID | None = None


class BulkActionResponse(BaseModel):
    """Result of a bulk action."""

    success: bool
    action: str
    affected_count: int
    errors: list[str] = Field(default_factory=list)


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


class UnifiedMailSummary(BaseModel):
    """Mail list item with account emoji for unified view."""

    id: uuid.UUID
    account_id: uuid.UUID
    account_emoji: str | None = None
    folder_id: uuid.UUID
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: Any | None = None
    received_at: datetime | None = None
    is_read: bool = False
    is_flagged: bool = False
    is_deleted: bool = False
    headers_synced: bool = False
    body_synced: bool = False
    snippet: str | None = None

    model_config = {"from_attributes": True}


class UnifiedMailListResponse(BaseModel):
    """Paginated unified mail list."""

    mails: list[UnifiedMailSummary]
    has_more: bool
    next_cursor: str | None = None


class EmojiUpdate(BaseModel):
    """Request to set an account emoji."""

    emoji: str | None = Field(
        default=None,
        max_length=10,
        description="Emoji character(s) for account identification",
    )


class UnifiedNameUpdate(BaseModel):
    """Request to set/clear a folder's unified name."""

    unified_name: str | None = Field(
        default=None,
        max_length=255,
        description="Unified name for cross-account folder merging",
    )


class UnifiedFolderOrderResponse(BaseModel):
    """Unified folder display order."""

    order: list[str]


class UnifiedFolderOrderUpdate(BaseModel):
    """Request to save unified folder display order."""

    order: list[str]
