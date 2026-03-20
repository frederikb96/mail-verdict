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

	function yTicks(): number[] {
		const max = maxTotal();
		if (max <= 5) return [0, max];
		const step = Math.ceil(max / 3);
		return [0, step, step * 2, max];
	}
</script>

<div class="bg-surface rounded-lg p-4">
	<h3 class="text-[11px] text-text-muted uppercase tracking-wide mb-3">Weekly Trend</h3>

	{#if data.length === 0}
		<p class="text-xs text-text-muted py-4 text-center">No trend data yet. Verdicts will appear after emails are classified.</p>
	{:else}
		<div class="flex gap-1">
			<!-- Y-axis labels -->
			<div class="flex flex-col justify-between h-32 pr-2 text-right">
				{#each yTicks().reverse() as tick}
					<span class="text-[9px] text-text-muted">{tick}</span>
				{/each}
			</div>

			<!-- Bars -->
			<div class="flex-1 flex items-end gap-1 h-32 border-l border-b border-border/30 pl-1">
				{#each data as point}
					<div class="flex-1 flex flex-col items-center gap-0.5 h-full justify-end">
						<!-- Value label -->
						{#if point.total > 0}
							<span class="text-[9px] text-text-muted">{point.total}</span>
						{/if}
						<!-- Corrections segment (stacked on top) -->
						{#if point.corrections > 0}
							<div
								class="w-full rounded-t bg-warn/60"
								style="height: {barHeight(point.corrections)}"
								title="{point.corrections} corrections"
							></div>
						{/if}
						<!-- Verdicts bar -->
						<div
							class="w-full rounded-t bg-accent/70"
							style="height: {barHeight(point.total - point.corrections)}"
							title="{point.total} verdicts, {(point.accuracy * 100).toFixed(0)}% accuracy"
						></div>
					</div>
				{/each}
			</div>
		</div>

		<!-- X-axis labels -->
		<div class="flex gap-1 mt-1 ml-8">
			{#each data as point}
				<div class="flex-1 text-center text-[9px] text-text-muted truncate">
					{formatWeek(point.week_start)}
				</div>
			{/each}
		</div>

		<!-- Legend -->
		<div class="flex items-center gap-4 mt-3 ml-8">
			<div class="flex items-center gap-1">
				<span class="w-2.5 h-2.5 rounded-sm bg-accent/70"></span>
				<span class="text-[10px] text-text-muted">Verdicts</span>
			</div>
			<div class="flex items-center gap-1">
				<span class="w-2.5 h-2.5 rounded-sm bg-warn/60"></span>
				<span class="text-[10px] text-text-muted">Corrections</span>
			</div>
		</div>
	{/if}
</div>
