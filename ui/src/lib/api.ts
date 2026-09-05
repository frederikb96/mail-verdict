/**
 * REST API client for MailVerdict backend.
 *
 * Platform-agnostic: uses standard fetch, works in browser and React Native.
 */

import type {
  AccountCreateRequest,
  AccountResponse,
  AccountUpdateRequest,
  AddressbookSummary,
  BulkActionRequest,
  BulkActionResponse,
  Calendar,
  CalendarCreateRequest,
  CalendarLinks,
  CalendarLinksUpdate,
  CalendarUpdateRequest,
  Contact,
  ContactCreateRequest,
  ContactListResponse,
  ContactPhotoIndexResponse,
  ContactSearchHit,
  ContactUpdateRequest,
  DavAccountCreateRequest,
  DavAccountResponse,
  DavAccountUpdateRequest,
  EventCreateRequest,
  EventDeleteRequest,
  EventInstance,
  EventListResponse,
  EventUpdateRequest,
  FeedbackResponse,
  FolderCreateRequest,
  FolderOrderResponse,
  FolderPrefsUpdate,
  FolderResponse,
  Identity,
  ImageExceptionCreate,
  ImageExceptionResponse,
  ImportInvitationRequest,
  Invitation,
  MessageActionRequest,
  MessageActionResponse,
  MessageDetail,
  MessageListResponse,
  MessageQuoteResponse,
  NotificationCountResponse,
  NotificationResponse,
  OutboxCreateRequest,
  OutboxCreateResult,
  OutboxResponse,
  PendingSendResponse,
  PipelineDocument,
  PipelineHealthEntry,
  PipelineRevisionSummary,
  PipelineRunResponse,
  PipelineTestRequest,
  PipelineTestResponse,
  PipelineWriteRequest,
  QueuePatchRequest,
  QueueResponse,
  RespondRequest,
  SearchField,
  SearchResponse,
  SearchStrictness,
  SelectionSnapshotResponse,
  SemanticSearchResponse,
  SpamReviewListResponse,
  StageCreateRequest,
  StageTypeOut,
  StageUpdateRequest,
  StatsResponse,
  SyncStatusResponse,
  ThreadResponse,
  UnifiedFolderOrderResponse,
  UnifiedFolderResponse,
  UnifiedMessageListResponse,
  VerdictResponse,
} from "@/types/api";

const BASE_URL = "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isFormData = init?.body instanceof FormData;
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: isFormData
      ? init?.headers
      : { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    // FastAPI error bodies are `{"detail": "..."}` -- surface that string
    // directly rather than the raw JSON envelope, wherever it's parseable.
    let message = text;
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed?.detail === "string") message = parsed.detail;
    } catch {
      // Not JSON -- use the raw body as-is.
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

type QsValue = string | number | boolean | undefined | null | string[];

function qs(params: Record<string, QsValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      // Repeated keys, e.g. folder_ids=a&folder_ids=b -- FastAPI's
      // list[...] Query param expects exactly this shape, not one
      // comma-joined value.
      for (const item of value) search.append(key, item);
    } else {
      search.append(key, String(value));
    }
  }
  const result = search.toString();
  return result ? `?${result}` : "";
}

export const api = {
  accounts: {
    list(): Promise<AccountResponse[]> {
      return request("/accounts");
    },
    get(id: string): Promise<AccountResponse> {
      return request(`/accounts/${id}`);
    },
    create(data: AccountCreateRequest): Promise<AccountResponse> {
      return request("/accounts", {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
    update(id: string, data: AccountUpdateRequest): Promise<AccountResponse> {
      return request(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      });
    },
    /** Permanently removes the account and its entire locally mirrored mailbox. */
    delete(id: string): Promise<void> {
      return request(`/accounts/${id}`, { method: "DELETE" });
    },
    syncStatus(id: string): Promise<SyncStatusResponse> {
      return request(`/accounts/${id}/sync-status`);
    },
    triggerSync(id: string): Promise<Record<string, string>> {
      return request(`/accounts/${id}/sync`, { method: "POST" });
    },
    setEmoji(id: string, emoji: string | null): Promise<{ emoji: string | null }> {
      return request(`/accounts/${id}/emoji`, {
        method: "PUT",
        body: JSON.stringify({ emoji }),
      });
    },
  },

  folders: {
    list(accountId: string): Promise<FolderResponse[]> {
      return request(`/accounts/${accountId}/folders`);
    },
    updatePrefs(
      folderId: string,
      data: FolderPrefsUpdate,
    ): Promise<FolderResponse> {
      return request(`/folders/${folderId}/prefs`, {
        method: "PATCH",
        body: JSON.stringify(data),
      });
    },
    create(
      accountId: string,
      data: FolderCreateRequest,
    ): Promise<FolderResponse> {
      return request(`/accounts/${accountId}/folders`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
    /**
     * Destroys every message in the folder on the mail server. Irreversible.
     *
     * messageCount has to match what the server currently counts in the
     * folder, so the caller can only delete a folder whose contents it has
     * actually seen -- a mismatch comes back as a 409 naming the real count.
     */
    delete(folderId: string, messageCount: number): Promise<void> {
      const query = `?confirm_message_count=${messageCount}`;
      return request(`/folders/${folderId}${query}`, { method: "DELETE" });
    },
  },

  imageExceptions: {
    list(accountId: string): Promise<ImageExceptionResponse[]> {
      return request(`/accounts/${accountId}/image-exceptions`);
    },
    create(
      accountId: string,
      data: ImageExceptionCreate,
    ): Promise<ImageExceptionResponse> {
      return request(`/accounts/${accountId}/image-exceptions`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
    delete(accountId: string, exceptionId: string): Promise<void> {
      return request(`/accounts/${accountId}/image-exceptions/${exceptionId}`, {
        method: "DELETE",
      });
    },
    check(
      accountId: string,
      sender: string,
    ): Promise<{ allowed: boolean; matched_by: string | null }> {
      return request(
        `/accounts/${accountId}/image-exceptions/check${qs({ sender })}`,
      );
    },
  },

  folderManagement: {
    getOrder(accountId: string): Promise<FolderOrderResponse> {
      return request(`/accounts/${accountId}/folder-order`);
    },
    updateOrder(
      accountId: string,
      order: string[],
    ): Promise<FolderOrderResponse> {
      return request(`/accounts/${accountId}/folder-order`, {
        method: "PUT",
        body: JSON.stringify({ order }),
      });
    },
  },

  mails: {
    list(params: {
      account_id: string;
      folder_id?: string;
      threaded?: boolean;
      before?: string;
      after?: string;
      around?: string;
      limit?: number;
    }): Promise<MessageListResponse> {
      const { account_id, ...rest } = params;
      return request(`/accounts/${account_id}/messages${qs(rest)}`);
    },

    get(id: string, loadImages?: boolean): Promise<MessageDetail> {
      return request(`/messages/${id}${qs({ load_images: loadImages })}`);
    },

    thread(id: string): Promise<ThreadResponse> {
      return request(`/messages/${id}/thread`);
    },

    action(id: string, body: MessageActionRequest): Promise<MessageActionResponse> {
      return request(`/messages/${id}/action`, {
        method: "POST",
        body: JSON.stringify(body),
      });
    },

    attachmentUrl(messageId: string, attachmentId: string): string {
      return `${BASE_URL}/messages/${messageId}/attachments/${attachmentId}`;
    },

    /** The message's RFC822 source as a .eml download. */
    rawUrl(messageId: string): string {
      return `${BASE_URL}/messages/${messageId}/raw`;
    },

    /** The message's body as safe-to-send HTML, for a reply or forward quote. */
    quote(id: string): Promise<MessageQuoteResponse> {
      return request(`/messages/${id}/quote`);
    },
  },

  verdicts: {
    get(mailId: string): Promise<VerdictResponse | null> {
      return request<VerdictResponse>(`/mails/${mailId}/verdict`).catch(
        (err) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        },
      );
    },

    list(params?: {
      account_id?: string;
      mail_id?: string;
      limit?: number;
    }): Promise<VerdictResponse[]> {
      return request(`/verdicts${qs(params ?? {})}`);
    },

    feedback(
      mailId: string,
      accountId: string,
      isSpam: boolean,
    ): Promise<FeedbackResponse> {
      return request(
        `/mails/${mailId}/feedback${qs({ account_id: accountId })}`,
        {
          method: "POST",
          body: JSON.stringify({ is_spam: isSpam }),
        },
      );
    },

    /** Every message currently classified spam with no user ruling yet,
     * across every account and folder, newest verdict first. */
    spamReview(params?: {
      before?: string;
      limit?: number;
    }): Promise<SpamReviewListResponse> {
      return request(`/verdicts/spam-review${qs(params ?? {})}`);
    },
  },

  outbox: {
    /**
     * Sends or saves a draft. Multipart when attachments are present so the
     * server can stream files straight into outbox_attachments without an
     * orphaned-upload lifecycle; JSON otherwise.
     *
     * A send with a nonzero undo window comes back as a PendingSendResponse
     * instead of an OutboxResponse -- not yet in outbox at all. Callers
     * distinguish the two by the presence of send_after.
     */
    create(data: OutboxCreateRequest, attachments?: File[]): Promise<OutboxCreateResult> {
      if (!attachments || attachments.length === 0) {
        return request("/outbox", {
          method: "POST",
          body: JSON.stringify(data),
        });
      }
      const form = new FormData();
      form.append("data", JSON.stringify(data));
      for (const file of attachments) {
        form.append("attachments", file, file.name);
      }
      return request("/outbox", { method: "POST", body: form });
    },

    list(params: {
      account_id?: string;
      status?: string;
    }): Promise<OutboxResponse[]> {
      return request(`/outbox${qs(params)}`);
    },

    /** Sends still inside their undo window, for the undo banner. */
    listPending(params: { account_id?: string }): Promise<PendingSendResponse[]> {
      return request(`/outbox/pending${qs(params)}`);
    },

    /** Cancels a send still inside its undo window. Throws (via request's
     * own non-2xx handling) once it's too late -- already sent, or
     * already cancelled. */
    cancelPending(id: string): Promise<void> {
      return request(`/outbox/pending/${id}/cancel`, { method: "POST" });
    },
  },

  stats: {
    get(accountId?: string): Promise<StatsResponse> {
      return request(`/stats${qs({ account_id: accountId })}`);
    },
  },

  search: {
    query(params: {
      q: string;
      account_id?: string;
      folder_ids?: string[];
      fields?: SearchField[];
      before?: string;
      limit?: number;
    }): Promise<SearchResponse> {
      return request(`/search${qs(params)}`);
    },
    /** Single-page: the strictness cutoff bounds the result set naturally,
     * so there is no limit/before to page with here. */
    semantic(params: {
      q: string;
      account_id?: string;
      folder_ids?: string[];
      strictness?: SearchStrictness;
    }): Promise<SemanticSearchResponse> {
      return request(`/embeddings/search${qs(params)}`);
    },
  },

  settings: {
    getAll(): Promise<Record<string, Record<string, unknown>>> {
      return request("/settings");
    },
    get(category: string): Promise<Record<string, unknown>> {
      return request(`/settings/${category}`);
    },
    update(
      category: string,
      data: Record<string, unknown>,
    ): Promise<Record<string, unknown>> {
      return request(`/settings/${category}`, {
        method: "PUT",
        body: JSON.stringify({ data }),
      });
    },
    import(
      data: Record<string, Record<string, unknown>>,
    ): Promise<Record<string, Record<string, unknown>>> {
      return request("/settings/import", {
        method: "POST",
        body: JSON.stringify({ data }),
      });
    },
  },

  messages: {
    bulkAction(
      accountId: string,
      body: BulkActionRequest,
    ): Promise<BulkActionResponse> {
      return request(`/accounts/${accountId}/messages/bulk-action`, {
        method: "POST",
        body: JSON.stringify(body),
      });
    },

    /** Mints a "select all matching" snapshot: an instant and a count from
     * one statement, so a following bulk-action's scope can carry the
     * instant back without the two ever disagreeing. */
    selection(
      accountId: string,
      params: { folder_id: string; filter?: "unread" | "all" },
    ): Promise<SelectionSnapshotResponse> {
      return request(`/accounts/${accountId}/messages/selection${qs(params)}`);
    },
  },

  unified: {
    setUnifiedName(
      accountId: string,
      folderId: string,
      unifiedName: string | null,
    ): Promise<FolderResponse> {
      return request(`/folders/${folderId}/prefs`, {
        method: "PATCH",
        body: JSON.stringify({ unified_name: unifiedName }),
      });
    },
    folders(): Promise<UnifiedFolderResponse[]> {
      return request("/unified/folders");
    },
    mails(params: {
      folder_name: string;
      before?: string;
      limit?: number;
    }): Promise<UnifiedMessageListResponse> {
      return request(`/unified/mails${qs(params)}`);
    },
    getFolderOrder(): Promise<UnifiedFolderOrderResponse> {
      return request("/unified/folder-order");
    },
    setFolderOrder(order: string[]): Promise<UnifiedFolderOrderResponse> {
      return request("/unified/folder-order", {
        method: "PUT",
        body: JSON.stringify({ order }),
      });
    },
  },

  notifications: {
    list(
      accountId: string,
      params?: { unacknowledged_only?: boolean; limit?: number },
    ): Promise<NotificationResponse[]> {
      return request(`/accounts/${accountId}/notifications${qs(params ?? {})}`);
    },
    unacknowledgedCount(accountId: string): Promise<NotificationCountResponse> {
      return request(`/accounts/${accountId}/notifications/unacknowledged-count`);
    },
    acknowledge(accountId: string, notificationId: number): Promise<void> {
      return request(`/accounts/${accountId}/notifications/${notificationId}/ack`, {
        method: "POST",
      });
    },
    acknowledgeAll(accountId: string): Promise<void> {
      return request(`/accounts/${accountId}/notifications/ack-all`, {
        method: "POST",
      });
    },
  },

  health(): Promise<{
    status: string;
    dependencies: Record<string, unknown>;
  }> {
    return request("/health");
  },

  pipeline: {
    get(): Promise<PipelineDocument> {
      return request("/pipeline");
    },
    replace(data: PipelineWriteRequest): Promise<PipelineDocument> {
      return request("/pipeline", { method: "PUT", body: JSON.stringify(data) });
    },
    stageTypes(): Promise<StageTypeOut[]> {
      return request("/pipeline/stage-types");
    },
    createStage(data: StageCreateRequest): Promise<PipelineDocument> {
      return request("/pipeline/stages", {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
    updateStage(
      stageId: string,
      data: StageUpdateRequest,
    ): Promise<PipelineDocument> {
      return request(`/pipeline/stages/${stageId}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      });
    },
    deleteStage(stageId: string, baseRevision?: number): Promise<PipelineDocument> {
      return request(
        `/pipeline/stages/${stageId}${qs({ base_revision: baseRevision })}`,
        { method: "DELETE" },
      );
    },
    reorderStages(
      order: string[],
      baseRevision?: number,
    ): Promise<PipelineDocument> {
      return request("/pipeline/stages/reorder", {
        method: "POST",
        body: JSON.stringify({ order, base_revision: baseRevision }),
      });
    },
    revisions(): Promise<PipelineRevisionSummary[]> {
      return request("/pipeline/revisions");
    },
    restoreRevision(revision: number): Promise<PipelineDocument> {
      return request(`/pipeline/revisions/${revision}/restore`, {
        method: "POST",
      });
    },
    /** One resolution entry per stage per account it applies to. */
    health(): Promise<PipelineHealthEntry[]> {
      return request("/pipeline/health");
    },
    /** Dry-run the whole pipeline against an existing message -- nothing applied or persisted. */
    test(data: PipelineTestRequest): Promise<PipelineTestResponse> {
      return request("/pipeline/test", {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
    testStage(
      stageId: string,
      data: PipelineTestRequest,
    ): Promise<Record<string, unknown>> {
      return request(`/pipeline/stages/${stageId}/test`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
  },

  queues: {
    list(): Promise<QueueResponse[]> {
      return request("/queues");
    },
    get(name: string): Promise<QueueResponse> {
      return request(`/queues/${name}`);
    },
    patch(name: string, data: QueuePatchRequest): Promise<QueueResponse> {
      return request(`/queues/${name}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      });
    },
  },

  runs: {
    list(params: {
      status?: string;
      account_id?: string;
      limit?: number;
      offset?: number;
    }): Promise<PipelineRunResponse[]> {
      return request(`/runs${qs(params)}`);
    },
    get(id: string): Promise<PipelineRunResponse> {
      return request(`/runs/${id}`);
    },
    retry(id: string): Promise<PipelineRunResponse> {
      return request(`/runs/${id}/retry`, { method: "POST" });
    },
    /** "Why did this message get that treatment" -- every run for one message. */
    forMail(mailId: string): Promise<PipelineRunResponse[]> {
      return request(`/mails/${mailId}/runs`);
    },
  },

  identities: {
    list(accountId?: string): Promise<Identity[]> {
      return request(`/identities${qs({ account_id: accountId })}`);
    },
    create(data: {
      account_id: string;
      address: string;
      display_name?: string;
      is_default?: boolean;
    }): Promise<Identity> {
      return request("/identities", { method: "POST", body: JSON.stringify(data) });
    },
    update(
      id: string,
      data: { display_name?: string; is_default?: boolean },
    ): Promise<Identity> {
      return request(`/identities/${id}`, { method: "PATCH", body: JSON.stringify(data) });
    },
    delete(id: string): Promise<void> {
      return request(`/identities/${id}`, { method: "DELETE" });
    },
  },

  davAccounts: {
    list(): Promise<DavAccountResponse[]> {
      return request("/dav-accounts");
    },
    get(id: string): Promise<DavAccountResponse> {
      return request(`/dav-accounts/${id}`);
    },
    create(data: DavAccountCreateRequest): Promise<DavAccountResponse> {
      return request("/dav-accounts", { method: "POST", body: JSON.stringify(data) });
    },
    update(id: string, data: DavAccountUpdateRequest): Promise<DavAccountResponse> {
      return request(`/dav-accounts/${id}`, { method: "PATCH", body: JSON.stringify(data) });
    },
    delete(id: string): Promise<void> {
      return request(`/dav-accounts/${id}`, { method: "DELETE" });
    },
    triggerSync(id: string): Promise<Record<string, string>> {
      return request(`/dav-accounts/${id}/sync`, { method: "POST" });
    },
  },

  calendars: {
    list(): Promise<Calendar[]> {
      return request("/calendars");
    },
    create(data: CalendarCreateRequest): Promise<Calendar> {
      return request("/calendars", { method: "POST", body: JSON.stringify(data) });
    },
    update(id: string, data: CalendarUpdateRequest): Promise<Calendar> {
      return request(`/calendars/${id}`, { method: "PATCH", body: JSON.stringify(data) });
    },
    /**
     * Destroys every event in the calendar on the mail server. Irreversible.
     *
     * eventCount has to match what the server currently counts in the
     * calendar, so the caller can only delete one whose contents it has
     * actually seen -- a mismatch comes back as a 409 naming the real count.
     */
    delete(id: string, eventCount: number): Promise<void> {
      const query = `?confirm_event_count=${eventCount}`;
      return request(`/calendars/${id}${query}`, { method: "DELETE" });
    },
    links: {
      get(): Promise<CalendarLinks> {
        return request("/calendar/links");
      },
      update(data: CalendarLinksUpdate): Promise<CalendarLinks> {
        return request("/calendar/links", { method: "PUT", body: JSON.stringify(data) });
      },
    },
  },

  events: {
    /** One calendar-month chunk, e.g. "2026-09", across every visible calendar
     * unless `calendars` narrows it. Omitting `calendars` means "all visible". */
    list(params: { month: string; calendars?: string[] }): Promise<EventListResponse> {
      return request(
        `/calendar/events${qs({ month: params.month, calendars: params.calendars?.join(",") })}`,
      );
    },
    get(objectId: string, recurrenceId?: string): Promise<EventInstance> {
      return request(`/calendar/events/${objectId}${qs({ recurrence_id: recurrenceId })}`);
    },
    create(data: EventCreateRequest): Promise<EventInstance> {
      return request("/calendar/events", { method: "POST", body: JSON.stringify(data) });
    },
    update(objectId: string, data: EventUpdateRequest): Promise<EventInstance> {
      return request(`/calendar/events/${objectId}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      });
    },
    delete(objectId: string, data?: EventDeleteRequest): Promise<void> {
      return request(`/calendar/events/${objectId}`, {
        method: "DELETE",
        body: data ? JSON.stringify(data) : undefined,
      });
    },
    respond(objectId: string, data: RespondRequest): Promise<EventInstance> {
      return request(`/calendar/events/${objectId}/respond`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
  },

  invitations: {
    get(messageId: string): Promise<Invitation> {
      return request(`/calendar/invitations/${messageId}`);
    },
    import(messageId: string, data: ImportInvitationRequest): Promise<Invitation> {
      return request(`/calendar/invitations/${messageId}/import`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
  },

  addressbooks: {
    list(): Promise<AddressbookSummary[]> {
      return request("/addressbooks");
    },
  },

  contacts: {
    list(params?: {
      addressbook_id?: string;
      q?: string;
      limit?: number;
      cursor?: string;
    }): Promise<ContactListResponse> {
      return request(`/contacts${qs(params ?? {})}`);
    },
    get(id: string): Promise<Contact> {
      return request(`/contacts/${id}`);
    },
    /** One row per email address, so a person with several addresses is
     * several choices in the compose autocomplete. */
    search(q: string): Promise<ContactSearchHit[]> {
      return request(`/contacts/search${qs({ q })}`);
    },
    /** The one contact carrying this address, or null -- what a sender's
     * avatar/name lookup resolves against. */
    resolveByEmail(email: string): Promise<Contact | null> {
      return request(`/contacts/resolve${qs({ email })}`);
    },
    /** The whole address book's sender-avatar photos in one request --
     * what a mail or search list reads from, never fetched per row.
     * `accountId` gates a `kind="url"` photo through that account's own
     * remote-content allowlist; omit it where no one account applies. */
    photoIndex(accountId?: string | null): Promise<ContactPhotoIndexResponse> {
      return request(`/contacts/photo-index${qs({ account_id: accountId ?? undefined })}`);
    },
    create(data: ContactCreateRequest): Promise<Contact> {
      return request("/contacts", { method: "POST", body: JSON.stringify(data) });
    },
    update(id: string, data: ContactUpdateRequest): Promise<Contact> {
      return request(`/contacts/${id}`, { method: "PATCH", body: JSON.stringify(data) });
    },
    delete(id: string): Promise<void> {
      return request(`/contacts/${id}`, { method: "DELETE" });
    },
  },
};
