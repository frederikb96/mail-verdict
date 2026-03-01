<script lang="ts">
	import { accounts } from '$lib/stores';
	import { api } from '$lib/api';
	import { onMount } from 'svelte';

	let health = $state<{ status: string; dependencies: Record<string, unknown> } | null>(null);

	onMount(async () => {
		try {
			health = await api.health();
		} catch {
			health = null;
		}
	});

	function formatDate(dateStr: string): string {
		return new Date(dateStr).toLocaleString();
	}
</script>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<h1 class="text-xl font-semibold text-text-primary">Settings</h1>

	<!-- Health -->
	<section class="bg-surface rounded-lg p-4">
		<h2 class="text-sm font-medium text-text-primary mb-3">System Health</h2>
		{#if health}
			<div class="flex items-center gap-2 mb-3">
				<span
					class="w-2 h-2 rounded-full"
					class:bg-ham={health.status === 'ok'}
					class:bg-spam={health.status !== 'ok'}
				></span>
				<span class="text-sm text-text-secondary">{health.status}</span>
			</div>
			{#if health.dependencies}
				<div class="space-y-1">
					{#each Object.entries(health.dependencies) as [name, status]}
						<div class="flex items-center justify-between text-xs">
							<span class="text-text-muted">{name}</span>
							<span class="text-text-secondary">{typeof status === 'string' ? status : JSON.stringify(status)}</span>
						</div>
					{/each}
				</div>
			{/if}
		{:else}
			<p class="text-xs text-text-muted">Unable to reach API</p>
		{/if}
	</section>

	<!-- Accounts -->
	<section class="bg-surface rounded-lg p-4">
		<h2 class="text-sm font-medium text-text-primary mb-3">Accounts</h2>
		{#if $accounts.length === 0}
			<p class="text-xs text-text-muted">No accounts configured</p>
		{:else}
			<div class="space-y-3">
				{#each $accounts as acct}
					<div class="bg-surface-dark rounded-lg p-3 space-y-2">
						<div class="flex items-center justify-between">
							<span class="text-sm font-medium text-text-primary">{acct.name}</span>
							<span
								class={`text-[10px] px-1.5 py-0.5 rounded-full ${
									acct.is_active
										? 'bg-ham/15 text-ham'
										: 'bg-text-muted/15 text-text-muted'
								}`}
							>
								{acct.is_active ? 'Active' : 'Inactive'}
							</span>
						</div>
						<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
							<div>
								<span class="text-text-muted">IMAP: </span>
								<span class="text-text-secondary">{acct.imap_host}:{acct.imap_port}</span>
							</div>
							<div>
								<span class="text-text-muted">User: </span>
								<span class="text-text-secondary">{acct.imap_user}</span>
							</div>
							{#if acct.smtp_host}
								<div>
									<span class="text-text-muted">SMTP: </span>
									<span class="text-text-secondary">{acct.smtp_host}:{acct.smtp_port ?? 587}</span>
								</div>
							{/if}
							<div>
								<span class="text-text-muted">Created: </span>
								<span class="text-text-secondary">{formatDate(acct.created_at)}</span>
							</div>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</section>
</div>
