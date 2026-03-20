<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';
	interface Props { children: Snippet; class?: string; }
	let { children, class: className }: Props = $props();
</script>
<h3 class={cn('text-sm font-medium leading-none tracking-tight text-text-primary', className)}>{@render children()}</h3>
