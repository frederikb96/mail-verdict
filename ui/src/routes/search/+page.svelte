<script lang="ts">
	import { page } from '$app/stores';
	import { api } from '$lib/api';
	import { currentAccount } from '$lib/stores';
	import type { SearchResponse } from '$lib/types';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	let results = $state<SearchResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);

	onMount(() => {
		const q = $page.url.searchParams.get('q');
		const mode = $page.url.searchParams.get('mode') as 'semantic' | 'fulltext' | null;
		if (q) {
			search(q, mode ?? 'fulltext');
		}
	});

	async function search(q: string, mode: 'semantic' | 'fulltext') {
		loading = true;
		error = null;
		try {
			results = await api.search.query({
				q,
				account_id: $currentAccount?.id,
				mode
			});
		} catch (e) {
			error = e instanceof Error ? e.message : 'Search failed';
		} finally {
			loading = false;
		}
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return '';
		return new Date(dateStr).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function openResult(mailId: string) {
		const accountId = $currentAccount?.id ?? '';
		goto(`/mail/${mailId}?account_id=${accountId}`);
	}
</script>

<div class="h-full overflow-y-auto p-6 space-y-4">
	<h1 class="text-xl font-semibold text-text-primary">Search</h1>

	{#if loading}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">Searching...</div>
	{:else if error}
		<div class="flex items-center justify-center py-16 text-spam text-sm">{error}</div>
	{:else if results}
		<div class="flex items-center gap-2 text-xs text-text-muted">
			<span>{results.total} results for "{results.query}"</span>
			<span class="px-1.5 py-0.5 rounded bg-surface text-text-muted">{results.mode}</span>
		</div>

		{#if results.results.length === 0}
			<div class="py-12 text-center text-text-muted text-sm">No results found</div>
		{:else}
			<div class="space-y-1">
				{#each results.results as result}
					<button
						class="w-full text-left px-4 py-3 rounded-lg bg-surface hover:bg-surface-light transition-colors"
						onclick={() => openResult(result.mail_id)}
					>
						<div class="flex items-center justify-between gap-3">
							<span class="text-sm text-text-primary truncate">{result.subject ?? '(no subject)'}</span>
							<span class="text-[11px] text-text-muted whitespace-nowrap">{formatDate(result.received_at)}</span>
						</div>
						<div class="flex items-center gap-3 mt-1">
							<span class="text-xs text-text-secondary">{result.from_addr ?? ''}</span>
							{#if result.score > 0}
								<span class="text-[10px] text-text-muted">score: {result.score.toFixed(2)}</span>
							{/if}
						</div>
					</button>
				{/each}
			</div>
		{/if}
	{:else}
		<div class="py-12 text-center text-text-muted text-sm">Enter a search query</div>
	{/if}
</div>
