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

export interface MailSummary {
	id: string;
	account_id: string;
	folder_id: string;
	subject: string | null;
	from_addr: string | null;
	to_addrs: unknown;
	received_at: string | null;
	is_read: boolean;
	is_flagged: boolean;
	is_deleted: boolean;
}

export interface MailDetail extends MailSummary {
	uid: number;
	message_id: string | null;
	cc_addrs: unknown;
	bcc_addrs: unknown;
	body_text: string | null;
	body_html: string | null;
	raw_headers: Record<string, unknown> | null;
	size_bytes: number | null;
	dkim_pass: boolean | null;
	spf_pass: boolean | null;
	dmarc_pass: boolean | null;
	fetched_at: string;
	created_at: string;
	tags: TagResponse[];
	attachments: AttachmentSummary[];
}

export interface MailActionRequest {
	action: 'move' | 'mark_read' | 'mark_unread' | 'delete' | 'flag' | 'unflag';
	target_folder?: string;
}

export interface MailActionResponse {
	success: boolean;
	action: string;
	mail_id: string;
	message: string | null;
}

export interface SearchResult {
	mail_id: string;
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
	sync_lookback_days: number;
	embedding_lookback_days: number;
	spam_enabled: boolean;
	created_at: string;
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
	sync_lookback_days?: number;
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
	sync_lookback_days?: number;
	embedding_lookback_days?: number;
	spam_enabled?: boolean;
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

export interface FolderResponse {
	id: string;
	account_id: string;
	imap_name: string;
	display_name: string | null;
	special_use: string | null;
	subscribed: boolean;
	last_synced_at: string | null;
}

export interface VerdictResponse {
	id: string;
	mail_id: string;
	is_spam: boolean;
	model_used: string | null;
	reasoning: string | null;
	source: string;
	created_at: string;
}

export interface FeedbackResponse {
	success: boolean;
	mail_id: string;
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
	mail_count: number;
}

export interface StatsResponse {
	total_mails: number;
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

export interface SSEEvent {
	event: string;
	detail?: string;
	account_id?: string;
	folder_id?: string;
	mail_id?: string;
	uid?: number;
	message_id?: string;
	is_spam?: boolean;
	source?: string;
	status?: string;
	timestamp: string;
	folder_name?: string;
	folder_index?: number;
	folder_total?: number;
	folder_count?: number;
	synced?: number;
	total_messages?: number;
	new_mails?: number;
	errors?: number;
	duration_s?: number;
	error_message?: string;
}
