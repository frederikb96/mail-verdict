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
  subject: string | null;
  from_addr: string | null;
  to_addrs: string | string[] | null;
  received_at: string | null;
  is_seen: boolean;
  is_flagged: boolean;
  is_answered: boolean;
  is_draft: boolean;
  is_deleted: boolean;
  deleted_at: string | null;
  snippet: string | null;
}

export interface MessageListResponse {
  messages: MessageSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface MessageDetail extends MessageSummary {
  imap_uid: number;
  message_id: string | null;
  cc_addrs: string | string[] | null;
  bcc_addrs: string | string[] | null;
  reply_to: string | null;
  in_reply_to: string | null;
  body_text: string | null;
  body_html: string | null;
  raw_headers: Record<string, unknown> | null;
  size_bytes: number | null;
  keywords: string[];
  has_blocked_images: boolean;
  images_allowed: boolean;
  created_at: string;
  tags: TagResponse[];
  attachments: AttachmentSummary[];
}

export interface MessageActionRequest {
  action:
    | "move"
    | "mark_read"
    | "mark_unread"
    | "delete"
    | "flag"
    | "unflag"
    | "archive"
    | "spam";
  target_folder?: string;
  target_folder_id?: string;
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
  score: number;
  source: string;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  mode: string;
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
  embedding_lookback_days: number;
  spam_enabled: boolean;
  folder_mapping: Record<string, string | null> | null;
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
  embedding_lookback_days?: number;
  spam_enabled?: boolean;
}

export interface AccountUpdateRequest {
  name?: string;
  imap_host?: string;
  imap_port?: number;
  imap_user?: string;
  imap_password?: string;
  smtp_host?: string;
  smtp_port?: number;
  smtp_user?: string;
  smtp_password?: string;
  is_active?: boolean;
  embedding_lookback_days?: number;
  spam_enabled?: boolean;
}

export interface FolderResponse {
  id: string;
  account_id: string;
  imap_name: string;
  display_name: string | null;
  special_use: string | null;
  mailbox_id: string | null;
  exists_count: number;
  unified_name: string | null;
  subscribed: boolean;
  is_visible: boolean;
  last_synced_at: string | null;
  sync_error: string | null;
  created_at: string | null;
  unread_count: number;
  total_count: number;
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

export interface JobStatus {
  name: string;
  account_id: string | null;
  status: string;
  cursor: Record<string, unknown> | null;
  last_run_at: string | null;
  error_count: number;
  last_error: string | null;
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
  embedding_count: number;
}

export interface SSEEvent {
  event_type?: string;
  account_id?: string;
  folder_id?: string;
  message_id?: string;
  imap_uid?: number;
  is_seen?: boolean;
  is_flagged?: boolean;
  timestamp: string;
  /** Sync state fields (backend sends phase/folder_name/elapsed_s/last_error) */
  status?: string;
  phase?: string;
  can_sync?: boolean;
  can_cancel?: boolean;
  current_folder?: string;
  folder_name?: string;
  folder_index?: number;
  folder_total?: number;
  synced?: number;
  total_messages?: number;
  new_mails?: number;
  errors?: number;
  duration_s?: number;
  elapsed_s?: number;
  error_message?: string;
  last_error?: string;
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

export interface IdleFolderItem {
  folder_id: string;
  imap_name: string;
  idle_enabled: boolean;
  idle_supported: boolean | null;
}

export interface IdleFolderToggleResponse {
  folder_id: string;
  enabled: boolean;
  success: boolean;
  error: string | null;
}

export interface IdleValidationResponse {
  folder_id: string;
  supported: boolean;
  error: string | null;
}

// --- Selection / bulk action types ---

export interface SelectionResponse {
  selected_ids: string[];
  count: number;
}

export interface SelectionToggle {
  message_id: string;
}

export interface SelectionRange {
  from_id: string;
  to_id: string;
  folder_id: string;
}

export interface SelectionAll {
  folder_id: string;
}

export interface BulkActionRequest {
  action:
    | "move"
    | "archive"
    | "spam"
    | "star"
    | "unstar"
    | "mark_read"
    | "mark_unread"
    | "delete";
  target_folder_id?: string;
}

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
  subject: string | null;
  from_addr: string | null;
  to_addrs: string | string[] | null;
  received_at: string | null;
  is_seen: boolean;
  is_flagged: boolean;
  is_answered: boolean;
  is_draft: boolean;
  is_deleted: boolean;
  deleted_at: string | null;
  snippet: string | null;
}

export interface UnifiedMessageListResponse {
  messages: UnifiedMessageSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface UnifiedFolderOrderResponse {
  order: string[];
}
