/** API response and request types matching backend Pydantic schemas. */

export interface TagResponse {
  tag_name: string;
  source: string;
}

export interface AttachmentSummary {
  id: string;
  filename: string | null;
  content_type: string | null;
  size_bytes: number | null;
}

export interface MessageSummary {
  id: string;
  account_id: string;
  folder_id: string;
  thread_id: string;
  subject: string | null;
  from_addr: string | null;
  to_addrs: string[] | null;
  received_at: string | null;
  is_seen: boolean;
  is_flagged: boolean;
  is_answered: boolean;
  is_draft: boolean;
  snippet: string | null;
  /** True while imap_uid is NULL — the message just moved and IMAP has not confirmed yet. */
  pending_sync: boolean;
  /** True when the server never fetched the body because it exceeded the size limit. */
  is_truncated: boolean;
  /** Only present when the list was fetched with threaded=true. */
  thread_count?: number;
  unread_in_thread?: number;
}

export interface MessageListResponse {
  messages: MessageSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface MessageDetail extends MessageSummary {
  message_id: string | null;
  cc_addrs: string[] | null;
  bcc_addrs: string[] | null;
  reply_to: string | null;
  in_reply_to: string | null;
  references: string[] | null;
  body_text: string | null;
  body_html: string | null;
  size_bytes: number | null;
  keywords: string[];
  has_blocked_images: boolean;
  images_allowed: boolean;
  created_at: string;
  tags: TagResponse[];
  attachments: AttachmentSummary[];
  verdict: VerdictResponse | null;
}

export interface ThreadResponse {
  messages: MessageDetail[];
}

export type MessageActionType =
  | "mark_read"
  | "mark_unread"
  | "flag"
  | "unflag"
  | "move"
  | "archive"
  | "trash"
  | "expunge"
  | "spam"
  | "not_spam"
  | "keyword_add"
  | "keyword_remove";

export interface MessageActionRequest {
  action: MessageActionType;
  target_folder_id?: string;
  keyword?: string;
}

export interface MessageActionResponse {
  success: boolean;
  action: string;
  message_id: string;
  message: string | null;
}

export interface SearchResult {
  message_id: string;
  subject: string | null;
  from_addr: string | null;
  received_at: string | null;
  snippet: string | null;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  query: string;
}

export interface AccountResponse {
  id: string;
  name: string;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  smtp_host: string | null;
  smtp_port: number | null;
  smtp_user: string | null;
  is_active: boolean;
  state: string;
  state_error: string | null;
  capabilities: Record<string, unknown> | null;
  emoji: string | null;
  spam_enabled: boolean;
  folder_order: string[] | null;
  created_at: string;
  updated_at: string;
}

export interface AccountCreateRequest {
  name: string;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  imap_password?: string;
  smtp_host?: string;
  smtp_port?: number;
  smtp_user?: string;
  smtp_password?: string;
  spam_enabled?: boolean;
}

/**
 * imap_host/imap_port/imap_user are insert-only under PostIMAP's contract --
 * changing the IMAP host requires deleting and re-adding the account.
 */
export interface AccountUpdateRequest {
  name?: string;
  imap_password?: string;
  smtp_host?: string;
  smtp_port?: number;
  smtp_user?: string;
  smtp_password?: string;
  is_active?: boolean;
  spam_enabled?: boolean;
}

export interface FolderResponse {
  id: string;
  account_id: string;
  imap_name: string;
  display_name: string | null;
  /** Effective special_use — folder_prefs.special_use_override coalesced with the server's own value. */
  special_use: string | null;
  mailbox_id: string | null;
  unified_name: string | null;
  is_visible: boolean;
  initial_sync_done: boolean;
  last_synced_at: string | null;
  sync_error: string | null;
  created_at: string | null;
  unread_count: number;
  total_count: number;
}

export interface FolderPrefsUpdate {
  is_visible?: boolean;
  display_name?: string | null;
  unified_name?: string | null;
  special_use_override?: string | null;
}

export interface VerdictResponse {
  id: string;
  message_id: string;
  is_spam: boolean;
  model_used: string | null;
  reasoning: string | null;
  source: string;
  created_at: string;
}

export interface FeedbackResponse {
  success: boolean;
  message_id: string;
  is_spam: boolean;
  message: string | null;
}

export interface WeeklyTrendPoint {
  week_start: string;
  total: number;
  corrections: number;
  accuracy: number;
}

export interface AccountSyncStatus {
  account_id: string;
  account_name: string;
  last_synced_at: string | null;
  folder_count: number;
  message_count: number;
}

export interface StatsResponse {
  total_messages: number;
  total_accounts: number;
  spam_caught: number;
  ham_count: number;
  false_positives: number;
  false_negatives: number;
  fp_rate: number;
  fn_rate: number;
  accuracy: number;
  weekly_trend: WeeklyTrendPoint[];
  account_sync: AccountSyncStatus[];
}

/** Names of `/api/events` SSE event types the client subscribes to. */
export type SSEEventType =
  | "mail.new"
  | "mail.updated"
  | "mail.deleted"
  | "folder.synced"
  | "folder.changed"
  | "account.changed"
  | "outbox.updated"
  | "verdict.issued"
  | "resync";

export interface SSEEvent {
  event_type?: string;
  /** The row's own id -- present on mail.*, verdict.issued (also as message_id) and outbox.updated. */
  id?: string;
  account_id?: string;
  folder_id?: string;
  folder_name?: string;
  message_id?: string;
  outbox_id?: string;
  is_seen?: boolean;
  is_flagged?: boolean;
  is_spam?: boolean;
  source?: string;
  status?: OutboxStatus;
  /** Outbox row kind, e.g. "send" or "draft". */
  kind?: string;
  /** Fields that changed on a mail.updated event, e.g. ["imap_uid"] confirms a move. */
  changed?: string[];
  /** "sync" = PostIMAP-originated, "app" = echo of our own write. */
  origin?: "sync" | "app";
  /** True on folder.synced when this was the initial backfill. */
  backfill?: boolean;
  timestamp: string;
  old_folder_id?: string;
}

export interface ImageExceptionResponse {
  id: string;
  type: "sender" | "domain";
  value: string;
  created_at: string;
}

export interface ImageExceptionCreate {
  type: "sender" | "domain";
  value: string;
}

export interface FolderOrderItem {
  folder_id: string;
  imap_name: string;
  display_name: string | null;
  special_use: string | null;
  is_visible: boolean;
  unread_count: number;
  total_count: number;
}

export interface FolderOrderResponse {
  folders: FolderOrderItem[];
}

export interface FolderOrderUpdate {
  order: string[];
}

// --- Bulk action types (client-held selection, server-side scope) ---

export type BulkActionType =
  | "move"
  | "mark_read"
  | "mark_unread"
  | "flag"
  | "unflag"
  | "archive"
  | "trash"
  | "expunge"
  | "spam"
  | "not_spam";

export interface BulkActionScope {
  folder_id: string;
  filter?: "unread" | "all";
  exclude_ids?: string[];
}

export type BulkActionTarget = { ids: string[] } | { scope: BulkActionScope };

export type BulkActionRequest = BulkActionTarget & {
  action: BulkActionType;
  target_folder_id?: string;
};

export interface BulkActionResponse {
  success: boolean;
  action: string;
  affected_count: number;
  errors: string[];
}

// --- Unified view types ---

export interface UnifiedFolderSource {
  account_id: string;
  account_name: string;
  account_emoji: string | null;
  folder_id: string;
  imap_name: string;
}

export interface UnifiedFolderResponse {
  unified_name: string;
  folders: UnifiedFolderSource[];
  unread_count: number;
  total_count: number;
}

export interface UnifiedMessageSummary {
  id: string;
  account_id: string;
  account_emoji: string | null;
  folder_id: string;
  thread_id: string;
  subject: string | null;
  from_addr: string | null;
  to_addrs: string[] | null;
  received_at: string | null;
  is_seen: boolean;
  is_flagged: boolean;
  is_answered: boolean;
  is_draft: boolean;
  snippet: string | null;
  pending_sync: boolean;
  is_truncated: boolean;
  thread_count?: number;
  unread_in_thread?: number;
}

export interface UnifiedMessageListResponse {
  messages: UnifiedMessageSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface UnifiedFolderOrderResponse {
  order: string[];
}

export interface SyncStatusResponse {
  account_id: string;
  state: string;
  state_error: string | null;
  last_full_sync: string | null;
  last_incr_sync: string | null;
  sync_tier: string | null;
  folders_synced: number;
  folders_total: number;
  messages_synced: number;
  error_count: number;
  last_error: string | null;
  updated_at: string | null;
}

// --- Outbox (send / draft) ---

export type OutboxKind = "send" | "draft";
export type OutboxStatus = "pending" | "processing" | "sent" | "failed" | "dead";

export interface OutboxCreateRequest {
  account_id: string;
  kind: OutboxKind;
  to: string[];
  cc?: string[];
  bcc?: string[];
  subject: string;
  body_text: string;
  body_html?: string;
  in_reply_to?: string;
  references?: string[];
}

export interface OutboxAttachmentSummary {
  id: string;
  filename: string;
  content_type: string | null;
  size_bytes: number | null;
}

export interface OutboxResponse {
  id: string;
  account_id: string;
  kind: OutboxKind;
  status: OutboxStatus;
  to: string[];
  cc: string[] | null;
  bcc: string[] | null;
  subject: string | null;
  error: string | null;
  attachments: OutboxAttachmentSummary[];
  created_at: string;
  updated_at: string;
}
