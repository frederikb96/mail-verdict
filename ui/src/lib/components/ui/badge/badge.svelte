<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';

	interface Props {
		variant?: 'default' | 'spam' | 'ham' | 'warn' | 'outline' | 'secondary';
		children: Snippet;
		class?: string;
	}

	let { variant = 'default', children, class: className }: Props = $props();

	const variants: Record<string, string> = {
		default: 'bg-accent/15 text-accent',
		spam: 'bg-spam/15 text-spam',
		ham: 'bg-ham/15 text-ham',
		warn: 'bg-warn/15 text-warn',
		outline: 'border border-border text-text-secondary',
		secondary: 'bg-surface-light text-text-secondary'
	};
</script>

<span class={cn(
	'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium',
	variants[variant],
	className
)}>
	{@render children()}
</span>
