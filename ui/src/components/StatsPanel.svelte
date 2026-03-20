<script lang="ts">
	import type { StatsResponse } from '$lib/types';
	import { Card, CardContent } from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';

	interface Props {
		stats: StatsResponse;
	}

	let { stats }: Props = $props();

	function pct(val: number): string {
		return (val * 100).toFixed(1) + '%';
	}
</script>

<div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Total Mails</div>
			<div class="text-2xl font-semibold text-text-primary mt-1">{stats.total_mails.toLocaleString()}</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Spam Caught</div>
			<div class="text-2xl font-semibold text-spam mt-1">{stats.spam_caught.toLocaleString()}</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Ham</div>
			<div class="text-2xl font-semibold text-ham mt-1">{stats.ham_count.toLocaleString()}</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Accuracy</div>
			<div class="text-2xl font-semibold text-accent mt-1">{pct(stats.accuracy)}</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="flex items-center gap-1">
				<div class="text-[11px] text-text-muted uppercase tracking-wide">False Positive Rate</div>
				<span class="text-text-muted cursor-help" title="Legitimate emails incorrectly flagged as spam, based on your corrections">
					<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
				</span>
			</div>
			<div class="text-xl font-medium mt-1" class:text-warn={stats.fp_rate > 0.05} class:text-text-primary={stats.fp_rate <= 0.05}>
				{pct(stats.fp_rate)}
			</div>
			<div class="text-[10px] text-text-muted mt-0.5">{stats.false_positives} corrections</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="flex items-center gap-1">
				<div class="text-[11px] text-text-muted uppercase tracking-wide">False Negative Rate</div>
				<span class="text-text-muted cursor-help" title="Spam emails that were missed and reached your inbox, based on your corrections">
					<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
				</span>
			</div>
			<div class="text-xl font-medium mt-1" class:text-warn={stats.fn_rate > 0.05} class:text-text-primary={stats.fn_rate <= 0.05}>
				{pct(stats.fn_rate)}
			</div>
			<div class="text-[10px] text-text-muted mt-0.5">{stats.false_negatives} corrections</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Accounts</div>
			<div class="text-2xl font-semibold text-text-primary mt-1">{stats.total_accounts}</div>
		</CardContent>
	</Card>

	<Card>
		<CardContent class="p-4">
			<div class="text-[11px] text-text-muted uppercase tracking-wide">Account Sync</div>
			<div class="mt-2 space-y-1.5">
				{#each stats.account_sync as sync}
					<div class="flex items-center justify-between text-[11px]">
						<span class="text-text-secondary truncate">{sync.account_name}</span>
						<Badge variant="secondary">{sync.mail_count} mails</Badge>
					</div>
				{/each}
				{#if stats.account_sync.length === 0}
					<span class="text-[10px] text-text-muted">No accounts syncing</span>
				{/if}
			</div>
		</CardContent>
	</Card>
</div>
