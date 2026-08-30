/**
 * REST API client for MailVerdict backend.
 *
 * Platform-agnostic: uses standard fetch, works in browser and React Native.
 */

import type {
  AccountCreateRequest,
  AccountResponse,
  AccountUpdateRequest,
  BulkActionRequest,
  BulkActionResponse,
  FeedbackResponse,
  FolderCreateRequest,
  FolderOrderResponse,
  FolderPrefsUpdate,
  FolderResponse,
  ImageExceptionCreate,
  ImageExceptionResponse,
  MessageActionRequest,
  MessageActionResponse,
  MessageDetail,
  MessageListResponse,
  NotificationCountResponse,
  NotificationResponse,
  OutboxCreateRequest,
  OutboxResponse,
  SearchResponse,
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
    throw new ApiError(res.status, text);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

function qs(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return "";
  return (
    "?" +
    new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()
  );
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
    /** Destroys every message in the folder on the mail server. Irreversible. */
    delete(folderId: string): Promise<void> {
      return request(`/folders/${folderId}`, { method: "DELETE" });
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
  },

  outbox: {
    /**
     * Sends or saves a draft. Multipart when attachments are present so the
     * server can stream files straight into outbox_attachments without an
     * orphaned-upload lifecycle; JSON otherwise.
     */
    create(data: OutboxCreateRequest, attachments?: File[]): Promise<OutboxResponse> {
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
  },

  stats: {
    get(accountId?: string): Promise<StatsResponse> {
      return request(`/stats${qs({ account_id: accountId })}`);
    },
  },

  search: {
    query(params: { q: string; account_id?: string }): Promise<SearchResponse> {
      return request(`/search${qs(params)}`);
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
};
