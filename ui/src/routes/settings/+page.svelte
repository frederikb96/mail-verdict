<script lang="ts">
	import { api } from '$lib/api';
	import { onMount } from 'svelte';

	const CATEGORIES = ['ai', 'spam', 'sync', 'retry'] as const;

	const FIELD_LABELS: Record<string, Record<string, string>> = {
		ai: { provider: 'AI Provider', model: 'Model', embedding_model: 'Embedding Model', embedding_dimensions: 'Embedding Dimensions', api_key: 'OpenAI API Key' },
		spam: { enabled: 'Global Spam Detection', excerpt_length: 'Excerpt Length', neighbor_count: 'Neighbor Count', auto_mark_read: 'Auto Mark Read' },
		sync: { enabled: 'Global Sync Enabled', poll_interval_seconds: 'Poll Interval (s)', idle_enabled: 'IDLE Enabled', idle_restart_seconds: 'IDLE Restart (s)' },
		retry: { max_retries: 'Max Retries', base_delay_seconds: 'Base Delay (s)', max_delay_seconds: 'Max Delay (s)', exponential_base: 'Exponential Base' }
	};

	const FIELD_DESCRIPTIONS: Record<string, Record<string, string>> = {
		ai: {
			provider: 'Currently the only supported provider',
			model: 'OpenAI model used for spam classification',
			embedding_model: 'Cannot be changed after first email is embedded',
			embedding_dimensions: 'Must match the embedding model output size',
			api_key: 'Your OpenAI API key. Changes take effect immediately (no restart needed).'
		},
		spam: {
			enabled: 'Master switch for spam detection. When off, overrides all per-account settings.',
			excerpt_length: 'Number of characters from the email body sent to the LLM',
			neighbor_count: 'Number of similar historical emails as context for the LLM',
			auto_mark_read: 'Automatically mark spam emails as read when moved to Junk folder'
		},
		sync: {
			enabled: 'Master switch for all sync. When off, disables all account syncing regardless of per-account settings.',
			poll_interval_seconds: 'How often to check for new emails via IMAP polling.',
			idle_enabled: 'Use IMAP IDLE for real-time push notifications.',
			idle_restart_seconds: 'Reconnect IDLE after this many seconds to prevent server timeouts.'
		},
		retry: {
			max_retries: 'Maximum retry attempts for failed operations',
			base_delay_seconds: 'Initial wait time before first retry',
			max_delay_seconds: 'Maximum wait between retries',
			exponential_base: 'Multiplier for each retry delay (e.g. 2 = 1s, 2s, 4s, 8s...)'
		}
	};

	const READ_ONLY_FIELDS: Record<string, Set<string>> = {
		ai: new Set(['provider', 'embedding_model', 'embedding_dimensions'])
	};

	const PASSWORD_FIELDS = new Set(['api_key']);

	let activeTab = $state<string>('ai');
	let allSettings = $state<Record<string, Record<string, unknown>>>({});
	let loading = $state(true);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let success = $state<string | null>(null);
	let showAdvanced = $state(false);

	onMount(async () => {
		await loadSettings();
	});

	async function loadSettings() {
		loading = true;
		error = null;
		try {
			allSettings = await api.settings.getAll();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function saveCategory(category: string) {
		saving = true;
		error = null;
		success = null;
		try {
			const data = allSettings[category] ?? {};
			allSettings[category] = await api.settings.update(category, data);
			success = `${category} settings saved`;
			setTimeout(() => { success = null; }, 2000);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	function fieldType(value: unknown): 'boolean' | 'number' | 'string' {
		if (typeof value === 'boolean') return 'boolean';
		if (typeof value === 'number') return 'number';
		return 'string';
	}

	function isReadOnly(category: string, key: string): boolean {
		return READ_ONLY_FIELDS[category]?.has(key) ?? false;
	}

	function isPassword(key: string): boolean {
		return PASSWORD_FIELDS.has(key);
	}

	function getVisibleFields(category: string): [string, unknown][] {
		const settings = allSettings[category] ?? {};
		const labels = FIELD_LABELS[category] ?? {};
		return Object.entries(settings).filter(([key]) => key in labels);
	}
</script>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<h1 class="text-xl font-semibold text-text-primary">Settings</h1>

	{#if error}
		<div class="px-4 py-2 rounded bg-spam/10 text-spam text-xs">{error}</div>
	{/if}
	{#if success}
		<div class="px-4 py-2 rounded bg-ham/10 text-ham text-xs">{success}</div>
	{/if}

	<!-- Tabs -->
	<div class="flex gap-1 border-b border-border">
		{#each CATEGORIES as cat}
			<button
				class="px-4 py-2 text-xs font-medium transition-colors border-b-2 -mb-px"
				class:border-accent={activeTab === cat}
				class:text-text-primary={activeTab === cat}
				class:border-transparent={activeTab !== cat}
				class:text-text-muted={activeTab !== cat}
				onclick={() => { activeTab = cat; }}
			>
				{cat.toUpperCase()}
			</button>
		{/each}
	</div>

	{#if loading}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading...</div>
	{:else}
		<!-- Info banners -->
		{#if activeTab === 'ai'}
			<div class="flex items-start gap-2 px-4 py-3 rounded-lg bg-accent/10 border border-accent/20">
				<svg class="w-4 h-4 text-accent mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
				<p class="text-xs text-text-secondary">Set your OpenAI API key below. Changes take effect immediately — no restart needed.</p>
			</div>
		{/if}
		{#if activeTab === 'spam'}
			<div class="flex items-start gap-2 px-4 py-3 rounded-lg bg-warn/10 border border-warn/20">
				<svg class="w-4 h-4 text-warn mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.732-.833-2.5 0L4.27 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
				<p class="text-xs text-text-secondary">Global master switch. When disabled, overrides all per-account spam settings.</p>
			</div>
		{/if}
		{#if activeTab === 'sync'}
			<div class="flex items-start gap-2 px-4 py-3 rounded-lg bg-accent/10 border border-accent/20">
				<svg class="w-4 h-4 text-accent mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
				<p class="text-xs text-text-secondary">Global sync defaults. Per-account sync can be toggled on the Accounts page. Disabling here stops all accounts.</p>
			</div>
		{/if}
		{#if activeTab === 'retry'}
			<div class="flex items-start gap-2 px-4 py-3 rounded-lg bg-surface-dark border border-border">
				<svg class="w-4 h-4 text-text-muted mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
				<p class="text-xs text-text-muted">Advanced: retry behavior for IMAP connections and API calls.</p>
			</div>
		{/if}

		<!-- Active category form -->
		{@const visibleFields = getVisibleFields(activeTab)}
		{@const descriptions = FIELD_DESCRIPTIONS[activeTab] ?? {}}
		<form
			class="bg-surface rounded-lg p-4 space-y-4"
			onsubmit={(e) => { e.preventDefault(); saveCategory(activeTab); }}
		>
			<div class="grid grid-cols-2 gap-4">
				{#each visibleFields as [key, value]}
					{@const type = fieldType(value)}
					{@const label = (FIELD_LABELS[activeTab] ?? {})[key] ?? key}
					{@const desc = descriptions[key] ?? ''}
					{@const readonly = isReadOnly(activeTab, key)}
					{@const pwd = isPassword(key)}
					<label class="space-y-1">
						<span class="text-xs text-text-muted">{label}</span>
						{#if readonly}
							<div class="text-sm text-text-primary py-1.5 flex items-center gap-2">
								{value}
								<span class="text-[10px] bg-surface-dark px-1.5 py-0.5 rounded text-text-muted">locked</span>
							</div>
						{:else if type === 'boolean'}
							<div class="flex items-center gap-2 pt-1">
								<input
									type="checkbox"
									checked={value === true}
									onchange={(e) => {
										const target = e.target as HTMLInputElement;
										allSettings[activeTab] = { ...allSettings[activeTab], [key]: target.checked };
									}}
									class="accent-accent"
								/>
								<span class="text-xs text-text-secondary">{value ? 'On' : 'Off'}</span>
							</div>
						{:else if type === 'number'}
							<input
								type="number"
								value={value as number}
								step="any"
								oninput={(e) => {
									const target = e.target as HTMLInputElement;
									allSettings[activeTab] = { ...allSettings[activeTab], [key]: Number(target.value) };
								}}
								class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none"
							/>
						{:else}
							<input
								type={pwd ? 'password' : 'text'}
								value={value as string}
								placeholder={pwd ? 'Enter key...' : ''}
								oninput={(e) => {
									const target = e.target as HTMLInputElement;
									allSettings[activeTab] = { ...allSettings[activeTab], [key]: target.value };
								}}
								class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none"
							/>
						{/if}
						{#if desc}
							<p class="text-[10px] text-text-muted leading-snug">{desc}</p>
						{/if}
					</label>
				{/each}
			</div>

			<button
				type="submit"
				class="px-4 py-1.5 text-xs bg-accent hover:bg-accent-hover text-white rounded transition-colors disabled:opacity-50"
				disabled={saving}
			>
				{saving ? 'Saving...' : 'Save'}
			</button>
		</form>
	{/if}
</div>
