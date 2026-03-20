<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';
	interface Props { children: Snippet; class?: string; }
	let { children, class: className }: Props = $props();
</script>
<div class={cn('p-4 pt-0', className)}>{@render children()}</div>
