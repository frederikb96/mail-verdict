<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api';
	import { currentAccount } from '$lib/stores';
	import StatsPanel from '../components/StatsPanel.svelte';
	import WeeklyChart from '../components/WeeklyChart.svelte';
	import type { StatsResponse } from '$lib/types';

	let stats = $state<StatsResponse | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		await loadStats();
	});

	async function loadStats() {
		loading = true;
		error = null;
		try {
			stats = await api.stats.get($currentAccount?.id);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load stats';
		} finally {
			loading = false;
		}
	}
</script>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<div class="flex items-center justify-between">
		<h1 class="text-xl font-semibold text-text-primary">Dashboard</h1>
		<button
			class="text-xs text-accent hover:text-accent-hover transition-colors"
			onclick={loadStats}
		>
			Refresh
		</button>
	</div>

	{#if loading}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading stats...</div>
	{:else if error}
		<div class="flex items-center justify-center py-16 text-spam text-sm">{error}</div>
	{:else if stats}
		<StatsPanel {stats} />
		<WeeklyChart data={stats.weekly_trend} />
	{/if}
</div>
