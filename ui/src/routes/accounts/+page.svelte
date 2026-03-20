<script lang="ts">
	import { api } from '$lib/api';
	import { onMount } from 'svelte';
	import type { AccountResponse, AccountCreateRequest } from '$lib/types';

	let accountList = $state<AccountResponse[]>([]);
	let loading = $state(true);
	let showForm = $state(false);
	let editId = $state<string | null>(null);
	let testResult = $state<Record<string, string> | null>(null);
	let testingId = $state<string | null>(null);
	let error = $state<string | null>(null);

	let form = $state<AccountCreateRequest>({
		name: '',
		imap_host: '',
		imap_port: 993,
		imap_user: '',
		imap_password: '',
		smtp_host: '',
		smtp_port: 465,
		smtp_user: '',
		smtp_password: '',
		sync_lookback_days: 180,
		embedding_lookback_days: 30,
		spam_enabled: false
	});

	onMount(loadAccounts);

	async function loadAccounts() {
		loading = true;
		try {
			accountList = await api.accounts.list();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load accounts';
		} finally {
			loading = false;
		}
	}

	function resetForm() {
		form = {
			name: '', imap_host: '', imap_port: 993, imap_user: '', imap_password: '',
			smtp_host: '', smtp_port: 465, smtp_user: '', smtp_password: '',
			sync_lookback_days: 180, embedding_lookback_days: 30, spam_enabled: false
		};
		editId = null;
		showForm = false;
	}

	function startEdit(acct: AccountResponse) {
		editId = acct.id;
		form = {
			name: acct.name,
			imap_host: acct.imap_host,
			imap_port: acct.imap_port,
			imap_user: acct.imap_user,
			smtp_host: acct.smtp_host ?? '',
			smtp_port: acct.smtp_port ?? 465,
			smtp_user: acct.smtp_user ?? '',
			sync_lookback_days: acct.sync_lookback_days,
			embedding_lookback_days: acct.embedding_lookback_days,
			spam_enabled: acct.spam_enabled
		};
		showForm = true;
	}

	async function handleSubmit() {
		error = null;
		try {
			if (editId) {
				const updateData = { ...form };
				if (!updateData.imap_password) delete updateData.imap_password;
				if (!updateData.smtp_password) delete updateData.smtp_password;
				await api.accounts.update(editId, updateData);
			} else {
				await api.accounts.create(form);
			}
			resetForm();
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		}
	}

	async function handleDelete(id: string) {
		try {
			await api.accounts.delete(id);
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Delete failed';
		}
	}

	async function handleTestConnection(id: string) {
		testingId = id;
		testResult = null;
		try {
			testResult = await api.accounts.testConnection(id);
		} catch (e) {
			testResult = { error: e instanceof Error ? e.message : 'Test failed' };
		} finally {
			testingId = null;
		}
	}

	async function toggleSpam(acct: AccountResponse) {
		try {
			await api.accounts.update(acct.id, { spam_enabled: !acct.spam_enabled });
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Toggle failed';
		}
	}

	const STATE_COLORS: Record<string, string> = {
		created: 'bg-text-muted/15 text-text-muted',
		syncing: 'bg-accent/15 text-accent',
		seeding: 'bg-warn/15 text-warn',
		active: 'bg-ham/15 text-ham',
		error: 'bg-spam/15 text-spam'
	};
</script>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<div class="flex items-center justify-between">
		<h1 class="text-xl font-semibold text-text-primary">Accounts</h1>
		<button
			class="px-3 py-1.5 text-xs bg-accent hover:bg-accent-hover text-white rounded transition-colors"
			onclick={() => { resetForm(); showForm = true; }}
		>
			Add Account
		</button>
	</div>

	{#if error}
		<div class="px-4 py-2 rounded bg-spam/10 text-spam text-xs">{error}</div>
	{/if}

	<!-- Account Form -->
	{#if showForm}
		<form class="bg-surface rounded-lg p-4 space-y-4" onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
			<h2 class="text-sm font-medium text-text-primary">{editId ? 'Edit' : 'New'} Account</h2>
			<div class="grid grid-cols-2 gap-3">
				<label class="space-y-1">
					<span class="text-xs text-text-muted">Name</span>
					<input type="text" bind:value={form.name} required class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">IMAP Host</span>
					<input type="text" bind:value={form.imap_host} required class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">IMAP Port</span>
					<input type="number" bind:value={form.imap_port} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">IMAP User</span>
					<input type="text" bind:value={form.imap_user} required class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">IMAP Password</span>
					<input type="password" bind:value={form.imap_password} placeholder={editId ? 'Leave blank to keep current' : ''} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none" />
					{#if editId}<span class="text-[10px] text-text-muted">Password is set</span>{/if}
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">SMTP Host</span>
					<input type="text" bind:value={form.smtp_host} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">SMTP Port</span>
					<input type="number" bind:value={form.smtp_port} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">SMTP User</span>
					<input type="text" bind:value={form.smtp_user} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">SMTP Password</span>
					<input type="password" bind:value={form.smtp_password} placeholder={editId ? 'Leave blank to keep current' : ''} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none" />
					{#if editId}<span class="text-[10px] text-text-muted">Password is set</span>{/if}
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">Sync Lookback (days)</span>
					<input type="number" bind:value={form.sync_lookback_days} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-text-muted">Embedding Lookback (days)</span>
					<input type="number" bind:value={form.embedding_lookback_days} class="w-full bg-surface-dark border border-border rounded px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none" />
				</label>
				<label class="flex items-center gap-2 pt-5">
					<input type="checkbox" bind:checked={form.spam_enabled} class="accent-accent" />
					<span class="text-xs text-text-secondary">Enable Spam Detection</span>
				</label>
			</div>
			<div class="flex gap-2">
				<button type="submit" class="px-4 py-1.5 text-xs bg-accent hover:bg-accent-hover text-white rounded transition-colors">
					{editId ? 'Save' : 'Create'}
				</button>
				<button type="button" class="px-4 py-1.5 text-xs text-text-muted hover:text-text-primary transition-colors" onclick={resetForm}>
					Cancel
				</button>
			</div>
		</form>
	{/if}

	<!-- Account List -->
	{#if loading}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">Loading...</div>
	{:else if accountList.length === 0}
		<div class="flex items-center justify-center py-16 text-text-muted text-sm">No accounts configured</div>
	{:else}
		<div class="space-y-3">
			{#each accountList as acct (acct.id)}
				<div class="bg-surface rounded-lg p-4 space-y-3">
					<div class="flex items-center justify-between">
						<div class="flex items-center gap-3">
							<span class="text-sm font-medium text-text-primary">{acct.name}</span>
							<span class={`text-[10px] px-1.5 py-0.5 rounded-full ${STATE_COLORS[acct.state] ?? 'bg-text-muted/15 text-text-muted'}`}>
								{acct.state}
							</span>
						</div>
						<div class="flex items-center gap-2">
							<button
								class="text-[11px] px-2 py-0.5 rounded border border-border text-text-secondary hover:text-text-primary transition-colors"
								onclick={() => startEdit(acct)}
							>
								Edit
							</button>
							<button
								class="text-[11px] px-2 py-0.5 rounded border border-border text-text-secondary hover:text-text-primary transition-colors"
								onclick={() => handleTestConnection(acct.id)}
								disabled={testingId === acct.id}
							>
								{testingId === acct.id ? 'Testing...' : 'Test'}
							</button>
							<button
								class="text-[11px] px-2 py-0.5 rounded border border-spam/30 text-spam hover:bg-spam/10 transition-colors"
								onclick={() => handleDelete(acct.id)}
							>
								Delete
							</button>
						</div>
					</div>

					<div class="grid grid-cols-2 lg:grid-cols-4 gap-x-4 gap-y-1 text-xs">
						<div><span class="text-text-muted">IMAP: </span><span class="text-text-secondary">{acct.imap_host}:{acct.imap_port}</span></div>
						<div><span class="text-text-muted">User: </span><span class="text-text-secondary">{acct.imap_user}</span></div>
						{#if acct.smtp_host}
							<div><span class="text-text-muted">SMTP: </span><span class="text-text-secondary">{acct.smtp_host}:{acct.smtp_port ?? 465}</span></div>
						{/if}
						<div><span class="text-text-muted">Lookback: </span><span class="text-text-secondary">{acct.sync_lookback_days}d sync / {acct.embedding_lookback_days}d embed</span></div>
					</div>

					<div class="flex items-center gap-4">
						<label class="flex items-center gap-2 cursor-pointer">
							<input
								type="checkbox"
								checked={acct.spam_enabled}
								onchange={() => toggleSpam(acct)}
								class="accent-accent"
							/>
							<span class="text-xs text-text-secondary">Spam Detection</span>
						</label>
					</div>

					{#if testResult && testingId === null}
						<div class="bg-surface-dark rounded p-2 space-y-1">
							{#each Object.entries(testResult) as [proto, status]}
								<div class="flex items-center gap-2 text-xs">
									<span class="text-text-muted uppercase">{proto}:</span>
									<span class={status === 'ok' ? 'text-ham' : 'text-spam'}>{status}</span>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
