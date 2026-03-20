<script lang="ts">
	import type { MailDetail as MailDetailType, VerdictResponse } from '$lib/types';
	import { api } from '$lib/api';
	import VerdictBadge from './VerdictBadge.svelte';
	import AuthBadge from './AuthBadge.svelte';
	import { onMount } from 'svelte';

	interface Props {
		mail: MailDetailType;
	}

	let { mail }: Props = $props();
	let verdict = $state<VerdictResponse | null>(null);
	let showHtml = $state(false);
	let loadRemoteImages = $state(false);
	let iframeRef = $state<HTMLIFrameElement | null>(null);

	onMount(() => {
		loadVerdict();
	});

	async function loadVerdict() {
		try {
			verdict = await api.verdicts.get(mail.id);
		} catch {
			verdict = null;
		}
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return '';
		return new Date(dateStr).toLocaleString(undefined, {
			weekday: 'short',
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function formatAddrs(addrs: unknown): string {
		if (!addrs) return '';
		if (Array.isArray(addrs)) return addrs.join(', ');
		if (typeof addrs === 'string') return addrs;
		return String(addrs);
	}

	function formatSize(bytes: number | null): string {
		if (bytes === null) return '';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}

	function prepareHtml(html: string): string {
		if (loadRemoteImages) {
			return html
				.replace(/data-x-src="([^"]*)"/g, 'src="$1"')
				.replace(/data-x-src='([^']*)'/g, "src='$1'")
				.replace(/data-x-bg="([^"]*)"/g, 'background="$1"');
		}
		return html;
	}

	$effect(() => {
		if (showHtml && mail.body_html && iframeRef) {
			const doc = iframeRef.contentDocument;
			if (doc) {
				const html = prepareHtml(mail.body_html);
				doc.open();
				doc.write(`
					<html><head><style>
						body { font-family: sans-serif; font-size: 14px; color: #e2e8f0; background: #1e293b; margin: 12px; }
						a { color: #60a5fa; }
						img { max-width: 100%; height: auto; }
					</style></head><body>${html}</body></html>
				`);
				doc.close();
			}
		}
	});
</script>

<div class="h-full flex flex-col overflow-hidden">
	<!-- Header -->
	<div class="p-4 border-b border-border space-y-3">
		<div class="flex items-start justify-between gap-3">
			<h1 class="text-lg font-medium text-text-primary leading-tight">
				{mail.subject ?? '(no subject)'}
			</h1>
			{#if mail.size_bytes}
				<span class="text-[10px] text-text-muted whitespace-nowrap flex-shrink-0">
					{formatSize(mail.size_bytes)}
				</span>
			{/if}
		</div>

		<div class="space-y-1 text-xs">
			<div class="flex gap-2">
				<span class="text-text-muted w-10">From</span>
				<span class="text-text-secondary">{mail.from_addr ?? '(unknown)'}</span>
			</div>
			<div class="flex gap-2">
				<span class="text-text-muted w-10">To</span>
				<span class="text-text-secondary">{formatAddrs(mail.to_addrs)}</span>
			</div>
			{#if mail.cc_addrs}
				<div class="flex gap-2">
					<span class="text-text-muted w-10">Cc</span>
					<span class="text-text-secondary">{formatAddrs(mail.cc_addrs)}</span>
				</div>
			{/if}
			<div class="flex gap-2">
				<span class="text-text-muted w-10">Date</span>
				<span class="text-text-secondary">{formatDate(mail.received_at)}</span>
			</div>
		</div>

		<!-- Auth + Verdict row -->
		<div class="flex items-center justify-between gap-4 pt-1">
			<AuthBadge dkim={mail.dkim_pass} spf={mail.spf_pass} dmarc={mail.dmarc_pass} />
			<VerdictBadge {verdict} mailId={mail.id} accountId={mail.account_id} onupdate={loadVerdict} />
		</div>

		<!-- Tags -->
		{#if mail.tags.length > 0}
			<div class="flex items-center gap-1.5 pt-1">
				{#each mail.tags as tag}
					<span class="px-1.5 py-0.5 rounded text-[10px] bg-surface-light text-text-muted">{tag.tag_name}</span>
				{/each}
			</div>
		{/if}

		<!-- Attachments -->
		{#if mail.attachments.length > 0}
			<div class="flex items-center gap-2 pt-1">
				<svg class="w-3.5 h-3.5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"/></svg>
				{#each mail.attachments as att}
					<span class="text-[10px] text-text-secondary">{att.filename ?? 'attachment'}</span>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Body toggle -->
	{#if mail.body_html}
		<div class="px-4 py-1.5 border-b border-border flex items-center gap-2">
			<button
				class="text-[11px] px-2 py-0.5 rounded transition-colors"
				class:bg-surface-light={!showHtml}
				class:text-text-primary={!showHtml}
				class:text-text-muted={showHtml}
				onclick={() => (showHtml = false)}
			>
				Text
			</button>
			<button
				class="text-[11px] px-2 py-0.5 rounded transition-colors"
				class:bg-surface-light={showHtml}
				class:text-text-primary={showHtml}
				class:text-text-muted={!showHtml}
				onclick={() => (showHtml = true)}
			>
				HTML
			</button>
			{#if showHtml && !loadRemoteImages}
				<button
					class="ml-auto text-[11px] px-2 py-0.5 rounded border border-warn/30 text-warn hover:bg-warn/10 transition-colors"
					onclick={() => { loadRemoteImages = true; }}
				>
					Load remote images
				</button>
			{/if}
			{#if showHtml && loadRemoteImages}
				<span class="ml-auto text-[10px] text-text-muted">Remote images loaded</span>
			{/if}
		</div>
	{/if}

	<!-- Body -->
	<div class="flex-1 overflow-y-auto p-4">
		{#if showHtml && mail.body_html}
			<iframe
				bind:this={iframeRef}
				class="w-full h-full border-0"
				sandbox="allow-same-origin"
				title="Email body"
			></iframe>
		{:else if mail.body_text}
			<pre class="text-sm text-text-secondary whitespace-pre-wrap font-sans leading-relaxed">{mail.body_text}</pre>
		{:else}
			<p class="text-sm text-text-muted">No body content</p>
		{/if}
	</div>
</div>
