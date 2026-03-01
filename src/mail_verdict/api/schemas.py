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

    model_config = {"from_attributes": True}


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
    fetched_at: datetime
    created_at: datetime
    tags: list[TagResponse] = Field(default_factory=list)
    attachments: list[AttachmentSummary] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MailActionRequest(BaseModel):
    """Request to perform an action on a mail."""

    action: Literal["move", "mark_read", "mark_unread", "delete", "flag", "unflag"] = Field(
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
    """Account summary."""

    id: uuid.UUID
    name: str
    imap_host: str
    imap_port: int
    imap_user: str
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    is_active: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    """Request to create an account."""

    name: str
    imap_host: str
    imap_port: int = 993
    imap_user: str
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    is_active: bool = True


class AccountUpdateRequest(BaseModel):
    """Request to update an account."""

    name: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_user: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    is_active: bool | None = None


# --- Folder schemas ---


class FolderResponse(BaseModel):
    """Folder summary."""

    id: uuid.UUID
    account_id: uuid.UUID
    imap_name: str
    display_name: str | None = None
    special_use: str | None = None
    subscribed: bool = True
    last_synced_at: datetime | None = None

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
