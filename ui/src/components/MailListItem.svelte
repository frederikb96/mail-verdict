<script lang="ts">
	import type { MailSummary } from '$lib/types';

	interface Props {
		mail: MailSummary;
		selected: boolean;
		onclick: () => void;
	}

	let { mail, selected, onclick }: Props = $props();

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return '';
		const d = new Date(dateStr);
		const now = new Date();
		const isToday = d.toDateString() === now.toDateString();
		if (isToday) {
			return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
		}
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function extractName(addr: string | null): string {
		if (!addr) return '(unknown)';
		const match = addr.match(/^"?([^"<]+)"?\s*</);
		if (match) return match[1].trim();
		return addr.split('@')[0];
	}
</script>

<button
	class="w-full text-left px-3 py-2.5 border-b border-border transition-colors hover:bg-surface-light"
	class:bg-surface-light={selected}
	class:border-l-2={selected}
	class:border-l-accent={selected}
	{onclick}
>
	<div class="flex items-center justify-between gap-2">
		<span
			class="text-sm truncate"
			class:font-semibold={!mail.is_read}
			class:text-text-primary={!mail.is_read}
			class:text-text-secondary={mail.is_read}
		>
			{extractName(mail.from_addr)}
		</span>
		<span class="text-[11px] text-text-muted whitespace-nowrap flex-shrink-0">
			{formatDate(mail.received_at)}
		</span>
	</div>
	<div
		class="text-xs mt-0.5 truncate"
		class:text-text-primary={!mail.is_read}
		class:text-text-muted={mail.is_read}
	>
		{mail.subject ?? '(no subject)'}
	</div>
	<div class="flex items-center gap-1.5 mt-1">
		{#if mail.is_flagged}
			<span class="w-1.5 h-1.5 rounded-full bg-warn" title="Flagged"></span>
		{/if}
		{#if !mail.is_read}
			<span class="w-1.5 h-1.5 rounded-full bg-accent" title="Unread"></span>
		{/if}
	</div>
</button>
