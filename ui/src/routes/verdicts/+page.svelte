<script lang="ts">
	import { api } from '$lib/api';
	import { currentAccount } from '$lib/stores';
	import { onMount } from 'svelte';
	import type { VerdictResponse } from '$lib/types';
	import { goto } from '$app/navigation';

	let verdicts = $state<VerdictResponse[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(loadVerdicts);

	async function loadVerdicts() {
		loading = true;
		error = null;
		try {
			verdicts = await api.verdicts.list({ account_id: $currentAccount?.id, limit: 100 });
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load verdicts';
		} finally {
			loading = false;
		}
	}

	function formatDate(dateStr: string): string {
		return new Date(dateStr).toLocaleString(undefined, {
			month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
		});
	}

	function openMail(mailId: string) {
		const accountId = $currentAccount?.id ?? '';
		goto(`/mail/${mailId}?account_id=${accountId}`);
	}
</script>

<div class="h-full overflow-y-auto p-6 space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="text-xl font-semibold text-text-primary">Classification Log</h1>
		<button
			class="text-xs text-accent hover:text-accent-hover transition-colors"
			onclick={loadVerdicts}
		>
			Refresh
		</button>
	</div>

	{#if loading}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading...</div>
	{:else if error}
		<div class="flex items-center justify-center py-16 text-spam text-sm">{error}</div>
	{:else if verdicts.length === 0}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">No verdicts yet</div>
	{:else}
		<div class="bg-surface rounded-lg overflow-hidden">
			<table class="w-full text-xs">
				<thead>
					<tr class="border-b border-border text-text-muted">
						<th class="text-left px-4 py-2 font-medium">Date</th>
						<th class="text-left px-4 py-2 font-medium">Verdict</th>
						<th class="text-left px-4 py-2 font-medium">Model</th>
						<th class="text-left px-4 py-2 font-medium">Source</th>
						<th class="text-left px-4 py-2 font-medium">Reasoning</th>
						<th class="text-right px-4 py-2 font-medium">Mail</th>
					</tr>
				</thead>
				<tbody>
					{#each verdicts as v (v.id)}
						<tr class="border-b border-border/50 hover:bg-surface-light/50 transition-colors">
							<td class="px-4 py-2 text-text-secondary whitespace-nowrap">{formatDate(v.created_at)}</td>
							<td class="px-4 py-2">
								<span class={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium ${v.is_spam ? 'bg-spam/15 text-spam' : 'bg-ham/15 text-ham'}`}>
									{v.is_spam ? 'Spam' : 'Ham'}
								</span>
							</td>
							<td class="px-4 py-2 text-text-muted">{v.model_used ?? '-'}</td>
							<td class="px-4 py-2 text-text-muted">{v.source}</td>
							<td class="px-4 py-2 text-text-secondary max-w-xs truncate">{v.reasoning ?? '-'}</td>
							<td class="px-4 py-2 text-right">
								<button
									class="text-accent hover:text-accent-hover transition-colors"
									onclick={() => openMail(v.mail_id)}
								>
									View
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
