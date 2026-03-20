<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';
	interface Props { children: Snippet; class?: string; }
	let { children, class: className }: Props = $props();
</script>
<div class={cn('rounded-lg border border-border bg-surface shadow-sm', className)}>
	{@render children()}
</div>
