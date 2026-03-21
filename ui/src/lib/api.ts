/**
 * REST API client for MailVerdict backend.
 *
 * Platform-agnostic: uses standard fetch, works in browser and React Native.
 */

import type {
  AccountCreateRequest,
  AccountResponse,
  AccountUpdateRequest,
  FeedbackResponse,
  FolderOrderResponse,
  FolderResponse,
  IdleFolderItem,
  IdleFolderToggleResponse,
  IdleValidationResponse,
  ImageExceptionCreate,
  ImageExceptionResponse,
  JobStatus,
  MailActionRequest,
  MailActionResponse,
  MailDetail,
  MailListResponse,
  SearchResponse,
  StatsResponse,
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
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
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
        method: "PUT",
        body: JSON.stringify(data),
      });
    },
    delete(id: string): Promise<void> {
      return request(`/accounts/${id}`, { method: "DELETE" });
    },
    testConnection(id: string): Promise<Record<string, string>> {
      return request(`/accounts/${id}/test-connection`, { method: "POST" });
    },
  },

  folders: {
    list(accountId: string): Promise<FolderResponse[]> {
      return request(`/accounts/${accountId}/folders`);
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
    toggleVisibility(
      accountId: string,
      folderId: string,
      isVisible: boolean,
    ): Promise<{ folder_id: string; is_visible: boolean }> {
      return request(
        `/accounts/${accountId}/folders/${folderId}/visibility`,
        {
          method: "PATCH",
          body: JSON.stringify({ is_visible: isVisible }),
        },
      );
    },
    autoDetectMapping(
      accountId: string,
    ): Promise<Record<string, string | null>> {
      return request(`/accounts/${accountId}/folder-mapping/auto-detect`, {
        method: "POST",
      });
    },
    getIdleFolders(accountId: string): Promise<IdleFolderItem[]> {
      return request(`/accounts/${accountId}/idle-folders`);
    },
    toggleIdle(
      accountId: string,
      folderId: string,
      enabled: boolean,
    ): Promise<IdleFolderToggleResponse> {
      return request(`/accounts/${accountId}/idle-folders`, {
        method: "PUT",
        body: JSON.stringify({ folder_id: folderId, enabled }),
      });
    },
    validateIdle(
      accountId: string,
      folderId: string,
    ): Promise<IdleValidationResponse> {
      return request(`/accounts/${accountId}/validate-idle`, {
        method: "POST",
        body: JSON.stringify({ folder_id: folderId }),
      });
    },
  },

  mails: {
    list(params: {
      account_id?: string;
      folder_id?: string;
      is_read?: boolean;
      before?: string;
      limit?: number;
    }): Promise<MailListResponse> {
      return request(`/mails${qs(params)}`);
    },

    get(
      id: string,
      accountId: string,
      loadImages?: boolean,
    ): Promise<MailDetail> {
      return request(
        `/mails/${id}${qs({ account_id: accountId, load_images: loadImages })}`,
      );
    },

    action(
      id: string,
      accountId: string,
      body: MailActionRequest,
    ): Promise<MailActionResponse> {
      return request(`/mails/${id}/action${qs({ account_id: accountId })}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
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

  stats: {
    get(accountId?: string): Promise<StatsResponse> {
      return request(`/stats${qs({ account_id: accountId })}`);
    },
  },

  search: {
    query(params: {
      q: string;
      account_id?: string;
      mode?: "semantic" | "fulltext";
    }): Promise<SearchResponse> {
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

  jobs: {
    list(): Promise<JobStatus[]> {
      return request("/jobs");
    },
    start(
      name: string,
      accountId?: string,
    ): Promise<Record<string, string>> {
      return request(`/jobs/${name}/start${qs({ account_id: accountId })}`, {
        method: "POST",
      });
    },
    stop(
      name: string,
      accountId?: string,
    ): Promise<Record<string, string>> {
      return request(`/jobs/${name}/stop${qs({ account_id: accountId })}`, {
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
