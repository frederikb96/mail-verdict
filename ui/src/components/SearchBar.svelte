<script lang="ts">
	import { goto } from '$app/navigation';

	let query = $state('');
	let mode = $state<'fulltext' | 'semantic'>('fulltext');

	function handleSubmit(e: Event) {
		e.preventDefault();
		if (!query.trim()) return;
		const params = new URLSearchParams({ q: query.trim(), mode });
		goto(`/search?${params.toString()}`);
	}
</script>

<form class="flex items-center gap-2" onsubmit={handleSubmit}>
	<div class="relative flex-1">
		<svg class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
		</svg>
		<input
			type="text"
			bind:value={query}
			placeholder="Search by subject, sender, or content..."
			class="w-full pl-8 pr-3 py-1.5 text-sm bg-surface-dark border border-border rounded text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none"
		/>
	</div>

	<select
		bind:value={mode}
		class="bg-surface-dark text-text-secondary text-xs rounded px-2 py-1.5 border border-border focus:border-accent focus:outline-none"
	>
		<option value="fulltext">Fulltext</option>
		<option value="semantic">Semantic</option>
	</select>

	<button
		type="submit"
		class="px-3 py-1.5 text-xs bg-accent hover:bg-accent-hover text-white rounded transition-colors"
	>
		Search
	</button>
</form>
