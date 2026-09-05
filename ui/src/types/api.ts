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
  /**
   * When this row entered the local mirror -- what a selection snapshot
   * compares against. Present on every list row; absent on a
   * MessageDetail, which is never itself a selection target.
   */
  mirrored_at?: string;
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

/** Which parts of a message the fulltext search endpoint scans. */
export type SearchField = "subject" | "from" | "to" | "body";

export interface SearchResult {
  message_id: string;
  account_id: string;
  folder_id: string;
  subject: string | null;
  from_addr: string | null;
  received_at: string | null;
  snippet: string | null;
  is_seen: boolean;
  is_flagged: boolean;
}

export interface SearchResponse {
  results: SearchResult[];
  has_more: boolean;
  next_cursor: string | null;
  query: string;
}

export interface SemanticSearchResult {
  message_id: string;
  account_id: string;
  folder_id: string;
  subject: string | null;
  from_addr: string | null;
  received_at: string | null;
  similarity: number;
  is_seen: boolean;
  is_flagged: boolean;
}

export interface SemanticSearchResponse {
  results: SemanticSearchResult[];
  query: string;
  model: string;
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

/**
 * IMAP has no parent concept -- parent_id, when given, names an existing
 * folder of the same account and the full path is built server-side by
 * joining onto it with the account's separator.
 */
export interface FolderCreateRequest {
  name: string;
  parent_id?: string;
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
  | "notification.new"
  | "pipeline.run_finished"
  | "calendar.object"
  | "calendar.collection"
  | "calendar.account"
  | "contact.object"
  | "contact.collection"
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
  /** Outbox status on outbox.updated; pipeline run status on pipeline.run_finished. */
  status?: OutboxStatus | string;
  /** Outbox row kind, e.g. "send" or "draft". */
  kind?: string;
  /** Present on pipeline.run_finished. */
  run_id?: string;
  halted_at?: string | null;
  /** Fields that changed on a mail.updated event, e.g. ["imap_uid"] confirms a move. */
  changed?: string[];
  /** "sync" = PostIMAP-originated, "app" = echo of our own write. */
  origin?: "sync" | "app";
  /** True on folder.synced when this was the initial backfill. */
  backfill?: boolean;
  timestamp: string;
  old_folder_id?: string;
  /** Present on calendar.* and contact.* events. */
  calendar_id?: string;
  dav_account_id?: string;
  addressbook_id?: string;
  /** Present on outbox.updated when the row is an iTIP reply, not a mail send. */
  itip?: "reply";
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
  /** From GET .../messages/selection -- required, never defaulted. */
  snapshot_at: string;
}

/** ids and scope may be given together (a predicate plus rows ticked on top of it); at least one is required. */
export type BulkActionTarget = { ids?: string[]; scope?: BulkActionScope };

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

/** GET .../messages/selection -- an instant and a count from one statement. */
export interface SelectionSnapshotResponse {
  snapshot_at: string;
  count: number;
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
  /** The identity to send as. Falls back to the account's default
   * identity, or accounts.imap_user if it has none at all. */
  identity_id?: string;
  /** The messages.id of a draft this row replaces -- editing or sending a
   * draft leaves no duplicate behind. Requires PostIMAP >= 1.4.0. */
  replaces_message_id?: string;
}

/** A message's body as safe-to-send HTML, for a reply or forward quote. */
export interface MessageQuoteResponse {
  html: string;
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

// --- Notification centre ---

export interface NotificationResponse {
  id: number;
  account_id: string;
  action: string;
  message_id: string | null;
  folder_id: string | null;
  outbox_id: string | null;
  error: string | null;
  detail: Record<string, unknown> | null;
  acknowledged_at: string | null;
  reverted_at: string | null;
  created_at: string;
}

export interface NotificationCountResponse {
  unacknowledged: number;
}

// --- Pipeline ---

export type StageRunsOn = "live" | "historical";

/** One registered stage type's JSON Schema for its `config` field. */
export interface StageTypeOut {
  type: string;
  runs_on: StageRunsOn[];
  schema: JsonSchema;
}

/** A (subset of) JSON Schema, enough to drive a generated form. */
export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
  additionalProperties?: boolean;
}

export interface JsonSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  enum?: string[];
  default?: unknown;
  anyOf?: JsonSchemaProperty[];
  items?: JsonSchemaProperty;
}

export interface StageOut {
  stage_id: string;
  type: string;
  name: string;
  config: Record<string, unknown>;
  enabled: boolean;
  halt: boolean;
  accounts: string[] | null;
}

export interface PipelineHealthEntry {
  stage_id: string;
  account_id: string;
  reference: string;
  ok: boolean;
  detail: string | null;
}

export interface PipelineDocument {
  revision: number;
  enabled: boolean;
  stages: StageOut[];
  warnings: PipelineHealthEntry[];
}

export interface PipelineWriteRequest {
  base_revision?: number | null;
  enabled?: boolean;
  stages?: Record<string, unknown>[];
}

export interface StageCreateRequest {
  stage_id: string;
  type: string;
  name?: string | null;
  config?: Record<string, unknown>;
  enabled?: boolean;
  halt?: boolean;
  accounts?: string[] | null;
  position?: number | null;
  base_revision?: number | null;
}

export interface StageUpdateRequest {
  name?: string | null;
  config?: Record<string, unknown> | null;
  enabled?: boolean | null;
  halt?: boolean | null;
  accounts?: string[] | null;
  base_revision?: number | null;
}

export interface PipelineRevisionSummary {
  revision: number;
  note: string | null;
  created_at: string;
}

export type PipelineTestOrigin = "live" | "historical";

export interface PipelineTestRequest {
  message_id: string;
  origin?: PipelineTestOrigin;
}

export interface PipelineTraceEntry {
  stage_id: string;
  type: string;
  matched?: boolean;
  halt?: boolean;
  detail?: string;
  usage?: { model?: string; latency_ms?: number } | null;
  effects?: Record<string, unknown>[];
  applied?: { effect: Record<string, unknown>; applied: boolean; detail?: string }[];
  [key: string]: unknown;
}

export interface PipelineTestResponse {
  status: string;
  skip_reason: string | null;
  trace: PipelineTraceEntry[];
}

export interface PipelineRunResponse {
  id: string;
  account_id: string;
  msg_key: string;
  message_id: string | null;
  origin: string;
  apply: boolean;
  status: string;
  skip_reason: string | null;
  attempts: number;
  pipeline_rev: number | null;
  halted_at_stage: string | null;
  failed_stage: string | null;
  last_error: string | null;
  trace: PipelineTraceEntry[];
  model_calls: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

// --- Queues ---

export interface QueueConcurrency {
  target: number;
  actual: number;
  max_allowed: number;
}

export interface CircuitStatusResponse {
  state: string;
  reason: string | null;
  since: string | null;
  retry_after: string | null;
}

export interface QueueResponse {
  name: string;
  state: "running" | "paused" | string;
  concurrency: QueueConcurrency;
  depth: Record<string, number>;
  circuit: CircuitStatusResponse;
}

export interface QueuePatchRequest {
  state?: "running" | "paused";
  concurrency?: number;
}

// --- Calendar and contacts ---
//
// The backend that serves these is being built in parallel; the shapes below
// follow the design's specified surface so the UI can be wired up as soon as
// the endpoints answer.

/** A named sending address on a mail account -- distinct from the account
 * itself, since one mailbox can send as several addresses. */
export interface Identity {
  id: string;
  account_id: string;
  address: string;
  display_name: string | null;
  is_default: boolean;
}

export type DavAccountState = "created" | "syncing" | "active" | "error" | "disabled";

export interface DavCollectionSummary {
  id: string;
  kind: "calendar" | "addressbook";
  display_name: string | null;
  sync_tier: string | null;
  initial_sync_done: boolean;
  total_count: number;
  backfill_total: number | null;
  last_synced_at: string | null;
}

export interface DavAccountResponse {
  id: string;
  name: string;
  discovery_url: string;
  username: string;
  is_active: boolean;
  state: DavAccountState;
  state_error: string | null;
  last_polled_at: string | null;
  collections: DavCollectionSummary[];
  created_at: string;
  updated_at: string;
}

export interface DavAccountCreateRequest {
  name: string;
  discovery_url: string;
  username: string;
  password: string;
}

export interface DavAccountUpdateRequest {
  name?: string;
  password?: string;
  is_active?: boolean;
}

/** calendar_prefs.intake is a plain database bool -- "not the intake
 * calendar" or "the intake calendar" -- so the wire type carries only
 * those two states. "Import without linking" is not a third calendar
 * state; it is per-message, ImportInvitationRequest's own `link` flag. */
export type CalendarIntake = "none" | "import_and_link";

export interface Calendar {
  id: string;
  dav_account_id: string;
  dav_account_name: string;
  display_name: string;
  /** The collection's own colour. */
  color: string;
  /** A per-user override; when set, this is what every surface renders. */
  color_override: string | null;
  /** Whether this calendar's events are drawn right now -- the sidebar's
   * own per-view checkbox. Meaningless while is_enabled is false. */
  is_visible: boolean;
  /** Whether this calendar is offered at all: the sidebar list, the
   * event editor's Calendar picker. Set only from the manage dialog. */
  is_enabled: boolean;
  read_only: boolean;
  /** The identity invitations addressed to it are attributed to and replied from. */
  identity_id: string | null;
  intake: CalendarIntake;
  supported_components: ("VEVENT" | "VTODO")[];
  sync_error: string | null;
  initial_sync_done: boolean;
  total_count: number;
}

export interface CalendarCreateRequest {
  dav_account_id: string;
  display_name: string;
  color?: string;
}

export interface CalendarUpdateRequest {
  display_name?: string;
  color_override?: string | null;
  is_visible?: boolean;
  is_enabled?: boolean;
  identity_id?: string | null;
  intake?: CalendarIntake;
}

export type Partstat = "needs-action" | "accepted" | "declined" | "tentative";
export type AttendeeRole = "chair" | "req-participant" | "opt-participant" | "non-participant";
export type EventStatus = "confirmed" | "tentative" | "cancelled";
/** "unknown" is the ITIP-reply-summary's own fallback once the outbox row
 * it points at has aged out of retention -- not a status a real outbox row
 * ever carries, so it lives here rather than on OutboxStatus itself. */
export type OutboxItipStatus = OutboxStatus | "unknown";

export interface EventAttendee {
  email: string;
  cn: string | null;
  partstat: Partstat;
  role: AttendeeRole;
}

/** The last outbox row this identity's `respond` produced for this object --
 * null until an RSVP has ever been sent, so a reply that never reaches the
 * organizer can be told apart from one that was never attempted. */
export interface OwnReply {
  partstat: Partstat;
  outbox_id: string;
  outbox_status: OutboxItipStatus;
  error: string | null;
  updated_at: string;
}

export interface EventInstance {
  object_id: string;
  /** Set on a modified occurrence of a recurring series; null on the master. */
  recurrence_id: string | null;
  calendar_id: string;
  uid: string;
  summary: string;
  dtstart: string;
  dtend: string;
  tz: string | null;
  all_day: boolean;
  location: string | null;
  description: string | null;
  status: EventStatus;
  sequence: number;
  /** null when the event does not repeat. */
  rrule: string | null;
  organizer: { email: string; cn: string | null } | null;
  attendees: EventAttendee[];
  /** This identity's own partstat on the event, when it is an attendee. */
  partstat: Partstat | null;
  is_recurring: boolean;
  is_exception: boolean;
  /** True while the write has not been confirmed by the server yet (etag IS NULL). */
  pending: boolean;
  sync_error: string | null;
  own_reply: OwnReply | null;
  source_message_id: string | null;
  read_only: boolean;
}

export type RecurrenceScope = "this" | "following" | "all";

export interface EventCreateRequest {
  calendar_id: string;
  summary: string;
  dtstart: string;
  dtend: string;
  tz?: string;
  all_day?: boolean;
  location?: string;
  description?: string;
  rrule?: string;
  attendees?: { email: string; cn?: string }[];
}

export interface EventUpdateRequest {
  calendar_id?: string;
  summary?: string;
  dtstart?: string;
  dtend?: string;
  all_day?: boolean;
  location?: string;
  description?: string;
  rrule?: string;
  /** Required when the object is a recurring instance. */
  scope?: RecurrenceScope;
  recurrence_id?: string;
}

export interface EventDeleteRequest {
  scope?: RecurrenceScope;
  recurrence_id?: string;
}

export interface EventListResponse {
  events: EventInstance[];
}

export interface RespondRequest {
  identity_id: string;
  partstat: "accepted" | "declined" | "tentative";
  comment?: string;
}

export type InvitationStatus =
  | "imported"
  | "updated"
  | "unlinked"
  | "cancelled"
  | "ignored_stale"
  | "failed"
  | "unauthorized"
  | "pending_review";

export interface Invitation {
  message_id: string;
  method: "REQUEST" | "REPLY" | "CANCEL" | "COUNTER";
  status: InvitationStatus;
  uid: string;
  summary: string;
  dtstart: string;
  dtend: string;
  all_day: boolean;
  location: string | null;
  organizer: { email: string; cn: string | null } | null;
  attendees: EventAttendee[];
  /** The address of the identity this invitation was addressed to, when found among the attendees. */
  own_address: string | null;
  sequence: number;
  calendar_id: string | null;
  calendar_name: string | null;
  object_id: string | null;
  error: string | null;
  own_reply: OwnReply | null;
  /** The message's own envelope sender -- compare against `organizer` on a pending_review card. */
  from_addr: string | null;
}

export interface ImportInvitationRequest {
  /** Required only when importing a genuinely new invitation -- confirming a pending_review
   * REQUEST/CANCEL resolves the target from the existing object itself and ignores this. */
  calendar_id?: string;
  /** Persist this calendar as the identity's default for future invitations from this address. */
  link?: boolean;
}

export interface CalendarLinkRow {
  identity_id: string;
  identity_address: string;
  account_id: string;
  calendar_ids: string[];
  receives_invitations_calendar_id: string | null;
}

export interface CalendarLinks {
  base_revision: number;
  rows: CalendarLinkRow[];
}

export interface CalendarLinksUpdate {
  base_revision: number;
  rows: { identity_id: string; calendar_ids: string[]; receives_invitations_calendar_id: string | null }[];
}

export interface ContactEmail {
  email: string;
  type: string | null;
}

/** `kind: "embedded"` -- `url` is a self-contained `data:` URI, already
 * mirrored, safe to render directly. `kind: "url"` -- `url` is a third
 * party's address; never put it in an `<img src>` without first running
 * it through the same remote-content allowlist any other remote image
 * does. */
export interface ContactPhoto {
  kind: "embedded" | "url";
  url: string;
}

export interface Contact {
  id: string;
  addressbook_id: string;
  addressbook_name: string;
  read_only: boolean;
  summary: string;
  emails: ContactEmail[];
  organization: string | null;
  title: string | null;
  phones: { number: string; type: string | null }[];
  addresses: { label: string | null; text: string }[];
  birthday: string | null;
  urls: string[];
  notes: string | null;
  categories: string[];
  photo: ContactPhoto | null;
}

export interface ContactListResponse {
  contacts: Contact[];
  has_more: boolean;
  next_cursor: string | null;
}

/** Already resolved server-side -- always safe to put straight into an
 * `<img src>`. See ContactPhotoIndexResponse. */
export interface ContactPhotoIndexEntry {
  contact_id: string;
  photo_url: string;
}

/** One request for the whole address book's sender-avatar photos, keyed
 * by lower-cased email -- meant to be cached with a long staleTime and
 * read synchronously as mail rows render, never re-requested per row. */
export interface ContactPhotoIndexResponse {
  by_email: Record<string, ContactPhotoIndexEntry>;
}

export interface ContactSearchHit {
  contact_id: string;
  name: string;
  email: string;
  source: "contact" | "recent" | "typed";
}

export interface ContactCreateRequest {
  addressbook_id: string;
  summary: string;
  emails: ContactEmail[];
  organization?: string;
  title?: string;
  phones?: { number: string; type?: string }[];
  addresses?: { label?: string; text: string }[];
  birthday?: string;
  urls?: string[];
  notes?: string;
  categories?: string[];
  /** A data: URI, as FileReader hands back an uploaded image. */
  photo_data_url?: string;
}

export type ContactUpdateRequest = Partial<Omit<ContactCreateRequest, "addressbook_id">>;

export interface AddressbookSummary {
  id: string;
  dav_account_id: string;
  dav_account_name: string;
  display_name: string;
  read_only: boolean;
  total_count: number;
}
