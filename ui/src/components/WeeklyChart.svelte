<script lang="ts">
	import type { WeeklyTrendPoint } from '$lib/types';

	interface Props {
		data: WeeklyTrendPoint[];
	}

	let { data }: Props = $props();

	function maxTotal(): number {
		if (data.length === 0) return 1;
		return Math.max(...data.map((d) => d.total), 1);
	}

	function barHeight(val: number): string {
		return `${Math.max((val / maxTotal()) * 100, 2)}%`;
	}

	function formatWeek(dateStr: string): string {
		const d = new Date(dateStr);
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

<div class="bg-surface rounded-lg p-4">
	<h3 class="text-[11px] text-text-muted uppercase tracking-wide mb-3">Weekly Trend</h3>

	{#if data.length === 0}
		<p class="text-xs text-text-muted py-4 text-center">No trend data</p>
	{:else}
		<div class="flex items-end gap-1 h-32">
			{#each data as point}
				<div class="flex-1 flex flex-col items-center gap-1 h-full justify-end">
					<!-- Corrections segment (stacked) -->
					{#if point.corrections > 0}
						<div
							class="w-full rounded-t bg-warn/60"
							style="height: {barHeight(point.corrections)}"
							title="{point.corrections} corrections"
						></div>
					{/if}
					<!-- Total bar -->
					<div
						class="w-full rounded-t bg-accent/70"
						style="height: {barHeight(point.total - point.corrections)}"
						title="{point.total} total"
					></div>
				</div>
			{/each}
		</div>

		<!-- Labels -->
		<div class="flex gap-1 mt-1">
			{#each data as point}
				<div class="flex-1 text-center text-[9px] text-text-muted truncate">
					{formatWeek(point.week_start)}
				</div>
			{/each}
		</div>

		<!-- Legend -->
		<div class="flex items-center gap-3 mt-3">
			<div class="flex items-center gap-1">
				<span class="w-2 h-2 rounded-sm bg-accent/70"></span>
				<span class="text-[10px] text-text-muted">Verdicts</span>
			</div>
			<div class="flex items-center gap-1">
				<span class="w-2 h-2 rounded-sm bg-warn/60"></span>
				<span class="text-[10px] text-text-muted">Corrections</span>
			</div>
		</div>
	{/if}
</div>
