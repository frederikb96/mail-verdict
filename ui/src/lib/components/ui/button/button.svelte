<script lang="ts">
	import { cn } from '$lib/utils';
	import type { Snippet } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';

	interface Props extends HTMLButtonAttributes {
		variant?: 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link';
		size?: 'default' | 'sm' | 'lg' | 'icon';
		children: Snippet;
		class?: string;
	}

	let { variant = 'default', size = 'default', children, class: className, ...rest }: Props = $props();

	const variants: Record<string, string> = {
		default: 'bg-accent text-white hover:bg-accent-hover shadow-sm',
		destructive: 'bg-spam text-white hover:bg-spam/90 shadow-sm',
		outline: 'border border-border bg-transparent hover:bg-surface-light text-text-primary',
		secondary: 'bg-surface-light text-text-primary hover:bg-surface-light/80',
		ghost: 'hover:bg-surface-light text-text-secondary hover:text-text-primary',
		link: 'text-accent underline-offset-4 hover:underline'
	};

	const sizes: Record<string, string> = {
		default: 'h-9 px-4 py-2 text-sm',
		sm: 'h-7 px-3 text-xs',
		lg: 'h-10 px-6 text-sm',
		icon: 'h-8 w-8'
	};
</script>

<button
	class={cn(
		'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent disabled:pointer-events-none disabled:opacity-50',
		variants[variant],
		sizes[size],
		className
	)}
	{...rest}
>
	{@render children()}
</button>
