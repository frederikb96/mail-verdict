<script lang="ts">
	import type { StatsResponse } from '$lib/types';

	interface Props {
		stats: StatsResponse;
	}

	let { stats }: Props = $props();

	function pct(val: number): string {
		return (val * 100).toFixed(1) + '%';
	}
</script>

<div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
	<!-- Total mails -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Total Mails</div>
		<div class="text-2xl font-semibold text-text-primary mt-1">{stats.total_mails.toLocaleString()}</div>
	</div>

	<!-- Spam caught -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Spam Caught</div>
		<div class="text-2xl font-semibold text-spam mt-1">{stats.spam_caught.toLocaleString()}</div>
	</div>

	<!-- Ham -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Ham</div>
		<div class="text-2xl font-semibold text-ham mt-1">{stats.ham_count.toLocaleString()}</div>
	</div>

	<!-- Accuracy -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Accuracy</div>
		<div class="text-2xl font-semibold text-accent mt-1">{pct(stats.accuracy)}</div>
	</div>

	<!-- FP Rate -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">False Positive Rate</div>
		<div class="text-xl font-medium mt-1" class:text-warn={stats.fp_rate > 0.05} class:text-text-primary={stats.fp_rate <= 0.05}>
			{pct(stats.fp_rate)}
		</div>
		<div class="text-[10px] text-text-muted mt-0.5">{stats.false_positives} total</div>
	</div>

	<!-- FN Rate -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">False Negative Rate</div>
		<div class="text-xl font-medium mt-1" class:text-warn={stats.fn_rate > 0.05} class:text-text-primary={stats.fn_rate <= 0.05}>
			{pct(stats.fn_rate)}
		</div>
		<div class="text-[10px] text-text-muted mt-0.5">{stats.false_negatives} total</div>
	</div>

	<!-- Accounts -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Accounts</div>
		<div class="text-2xl font-semibold text-text-primary mt-1">{stats.total_accounts}</div>
	</div>

	<!-- Sync status -->
	<div class="bg-surface rounded-lg p-4">
		<div class="text-[11px] text-text-muted uppercase tracking-wide">Account Sync</div>
		<div class="mt-2 space-y-1.5">
			{#each stats.account_sync as sync}
				<div class="flex items-center justify-between text-[11px]">
					<span class="text-text-secondary truncate">{sync.account_name}</span>
					<span class="text-text-muted">{sync.mail_count}</span>
				</div>
			{/each}
		</div>
	</div>
</div>
