import type {
	AccountResponse,
	FeedbackResponse,
	FolderResponse,
	MailActionRequest,
	MailActionResponse,
	MailDetail,
	MailSummary,
	SearchResponse,
	StatsResponse,
	VerdictResponse
} from './types';

const BASE_URL = '/api';

class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
		this.name = 'ApiError';
	}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${BASE_URL}${path}`, {
		headers: { 'Content-Type': 'application/json', ...init?.headers },
		...init
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new ApiError(res.status, text);
	}
	return res.json();
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
	const entries = Object.entries(params).filter(
		([, v]) => v !== undefined && v !== null && v !== ''
	);
	if (entries.length === 0) return '';
	return '?' + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString();
}

export const api = {
	accounts: {
		list(): Promise<AccountResponse[]> {
			return request('/accounts');
		}
	},

	folders: {
		list(accountId: string): Promise<FolderResponse[]> {
			return request(`/folders${qs({ account_id: accountId })}`);
		}
	},

	mails: {
		list(params: {
			account_id: string;
			folder_id?: string;
			is_read?: boolean;
			limit?: number;
			offset?: number;
		}): Promise<MailSummary[]> {
			return request(`/mails${qs(params)}`);
		},

		get(id: string, accountId: string): Promise<MailDetail> {
			return request(`/mails/${id}${qs({ account_id: accountId })}`);
		},

		action(id: string, accountId: string, body: MailActionRequest): Promise<MailActionResponse> {
			return request(`/mails/${id}/action${qs({ account_id: accountId })}`, {
				method: 'POST',
				body: JSON.stringify(body)
			});
		}
	},

	verdicts: {
		get(mailId: string): Promise<VerdictResponse | null> {
			return request(`/mails/${mailId}/verdict`).catch((err) => {
				if (err instanceof ApiError && err.status === 404) return null;
				throw err;
			});
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
			isSpam: boolean
		): Promise<FeedbackResponse> {
			return request(`/mails/${mailId}/feedback${qs({ account_id: accountId })}`, {
				method: 'POST',
				body: JSON.stringify({ is_spam: isSpam })
			});
		}
	},

	stats: {
		get(accountId?: string): Promise<StatsResponse> {
			return request(`/stats${qs({ account_id: accountId })}`);
		}
	},

	search: {
		query(params: {
			q: string;
			account_id?: string;
			mode?: 'semantic' | 'fulltext';
		}): Promise<SearchResponse> {
			return request(`/search${qs(params)}`);
		}
	},

	health(): Promise<{ status: string; dependencies: Record<string, unknown> }> {
		return request('/health');
	}
};
