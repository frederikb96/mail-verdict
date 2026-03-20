<script lang="ts">
	import { api } from '$lib/api';
	import { onMount } from 'svelte';

	const CATEGORIES = ['ai', 'spam', 'sync', 'retry'] as const;

	const FIELD_LABELS: Record<string, Record<string, string>> = {
		ai: { provider: 'AI Provider', model: 'Model', embedding_model: 'Embedding Model', embedding_dimensions: 'Embedding Dimensions' },
		spam: { enabled: 'Global Spam Detection', excerpt_length: 'Excerpt Length', neighbor_count: 'Neighbor Count', auto_mark_read: 'Auto Mark Read' },
		sync: { poll_interval_seconds: 'Poll Interval (s)', idle_enabled: 'IDLE Enabled', idle_restart_seconds: 'IDLE Restart (s)', lookback_days: 'Lookback Days', auto_detect_folders: 'Auto Detect Folders' },
		retry: { max_retries: 'Max Retries', base_delay_seconds: 'Base Delay (s)', max_delay_seconds: 'Max Delay (s)', exponential_base: 'Exponential Base' }
	};

	const FIELD_DESCRIPTIONS: Record<string, Record<string, string>> = {
		ai: {
			provider: 'Currently the only supported provider',
			model: 'OpenAI model used for spam classification',
			embedding_model: 'Cannot be changed after first email is embedded',
			embedding_dimensions: 'Must match the embedding model output size'
		},
		spam: {
			enabled: 'Master switch for spam detection. When off, no accounts are checked regardless of per-account settings.',
			excerpt_length: 'Number of characters from the email body sent to the LLM for classification',
			neighbor_count: 'Number of similar historical emails to include as context for the LLM',
			auto_mark_read: 'Automatically mark spam emails as read when moved to Junk folder'
		},
		sync: {
			poll_interval_seconds: 'How often to check for new emails via IMAP polling. Lower = more responsive, higher = less server load.',
			idle_enabled: 'Use IMAP IDLE for real-time push notifications when new mail arrives. Recommended for most servers.',
			idle_restart_seconds: 'Reconnect the IDLE connection after this many seconds to prevent server timeouts. Most servers require < 1800s.',
			lookback_days: 'How many days of historical email to sync when adding a new account.',
			auto_detect_folders: 'Automatically discover and sync all IMAP folders. When disabled, only INBOX is synced.'
		},
		retry: {
			max_retries: 'Maximum retry attempts for failed operations (IMAP connections, API calls)',
			base_delay_seconds: 'Initial wait time before the first retry attempt',
			max_delay_seconds: 'Maximum wait time between retries, regardless of exponential backoff',
			exponential_base: 'Multiplier for each retry delay. E.g., base=2 means delays of 1s, 2s, 4s, 8s...'
		}
	};

	const READ_ONLY_FIELDS: Record<string, Set<string>> = {
		ai: new Set(['provider'])
	};

	let activeTab = $state<string>('ai');
	let allSettings = $state<Record<string, Record<string, unknown>>>({});
	let loading = $state(true);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let success = $state<string | null>(null);

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
				<p class="text-xs text-text-secondary">Set your OpenAI API key below. The app needs a restart after changing the key for AI features to activate.</p>
			</div>
		{/if}
		{#if activeTab === 'spam'}
			<div class="flex items-start gap-2 px-4 py-3 rounded-lg bg-warn/10 border border-warn/20">
				<svg class="w-4 h-4 text-warn mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.732-.833-2.5 0L4.27 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
				<p class="text-xs text-text-secondary">This is the global master switch. Per-account spam detection can be toggled on the Accounts page. Both must be enabled for spam detection to work.</p>
			</div>
		{/if}

		<!-- Active category form -->
		{@const settings = allSettings[activeTab] ?? {}}
		{@const labels = FIELD_LABELS[activeTab] ?? {}}
		{@const descriptions = FIELD_DESCRIPTIONS[activeTab] ?? {}}
		<form
			class="bg-surface rounded-lg p-4 space-y-4"
			onsubmit={(e) => { e.preventDefault(); saveCategory(activeTab); }}
		>
			<div class="grid grid-cols-2 gap-4">
				{#each Object.entries(settings) as [key, value]}
					{@const type = fieldType(value)}
					{@const label = labels[key] ?? key}
					{@const desc = descriptions[key] ?? ''}
					{@const readonly = isReadOnly(activeTab, key)}
					<label class="space-y-1">
						<span class="text-xs text-text-muted">{label}</span>
						{#if readonly}
							<div class="text-sm text-text-primary py-1.5">{value}</div>
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
								type="text"
								value={value as string}
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
