import type { SSEEvent } from './types';

type SSEHandler = (event: SSEEvent) => void;

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;

class SSEClient {
	private source: EventSource | null = null;
	private handlers = new Map<string, Set<SSEHandler>>();
	private reconnectDelay = RECONNECT_DELAY_MS;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private accountId: string | null = null;

	connect(accountId?: string): void {
		this.disconnect();
		this.accountId = accountId ?? null;

		const url = accountId ? `/api/events?account_id=${accountId}` : '/api/events';
		this.source = new EventSource(url);

		this.source.onopen = () => {
			this.reconnectDelay = RECONNECT_DELAY_MS;
		};

		this.source.onerror = () => {
			this.source?.close();
			this.source = null;
			this.scheduleReconnect();
		};

		const eventTypes = ['new_mail', 'folder_change', 'flags_changed', 'verdict_issued', 'sync_status'];
		for (const type of eventTypes) {
			this.source.addEventListener(type, (e: MessageEvent) => {
				try {
					const data: SSEEvent = JSON.parse(e.data);
					this.emit(type, data);
					this.emit('*', data);
				} catch {
					// ignore parse errors
				}
			});
		}
	}

	disconnect(): void {
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		if (this.source) {
			this.source.close();
			this.source = null;
		}
	}

	on(event: string, handler: SSEHandler): () => void {
		if (!this.handlers.has(event)) {
			this.handlers.set(event, new Set());
		}
		this.handlers.get(event)!.add(handler);
		return () => this.handlers.get(event)?.delete(handler);
	}

	private emit(event: string, data: SSEEvent): void {
		const set = this.handlers.get(event);
		if (set) {
			for (const handler of set) {
				handler(data);
			}
		}
	}

	private scheduleReconnect(): void {
		this.reconnectTimer = setTimeout(() => {
			this.connect(this.accountId ?? undefined);
		}, this.reconnectDelay);
		this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
	}
}

export const sse = new SSEClient();
