const BASE_URL = "/api";
class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}
async function request(path, init) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text);
  }
  return res.json();
}
function qs(params) {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== void 0 && v !== null && v !== ""
  );
  if (entries.length === 0) return "";
  return "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString();
}
const api = {
  accounts: {
    list() {
      return request("/accounts");
    }
  },
  folders: {
    list(accountId) {
      return request(`/folders${qs({ account_id: accountId })}`);
    }
  },
  mails: {
    list(params) {
      return request(`/mails${qs(params)}`);
    },
    get(id, accountId) {
      return request(`/mails/${id}${qs({ account_id: accountId })}`);
    },
    action(id, accountId, body) {
      return request(`/mails/${id}/action${qs({ account_id: accountId })}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
    }
  },
  verdicts: {
    get(mailId) {
      return request(`/mails/${mailId}/verdict`).catch((err) => {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      });
    },
    list(params) {
      return request(`/verdicts${qs(params ?? {})}`);
    },
    feedback(mailId, accountId, isSpam) {
      return request(`/mails/${mailId}/feedback${qs({ account_id: accountId })}`, {
        method: "POST",
        body: JSON.stringify({ is_spam: isSpam })
      });
    }
  },
  stats: {
    get(accountId) {
      return request(`/stats${qs({ account_id: accountId })}`);
    }
  },
  search: {
    query(params) {
      return request(`/search${qs(params)}`);
    }
  },
  health() {
    return request("/health");
  }
};
export {
  api as a
};
