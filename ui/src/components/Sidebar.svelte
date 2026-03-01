<script lang="ts">
	import { accounts, currentAccount, folders, selectedFolder, sidebarCollapsed, foldersBySpecialUse } from '$lib/stores';
	import { api } from '$lib/api';

	const FOLDER_ICONS: Record<string, string> = {
		'\\Inbox': '\u{1F4E5}',
		'\\Sent': '\u{1F4E4}',
		'\\Drafts': '\u{1F4DD}',
		'\\Trash': '\u{1F5D1}',
		'\\Junk': '\u{26A0}',
		'\\Archive': '\u{1F4E6}'
	};

	function folderIcon(specialUse: string | null): string {
		if (specialUse && FOLDER_ICONS[specialUse]) return FOLDER_ICONS[specialUse];
		return '\u{1F4C1}';
	}

	function folderDisplayName(folder: { display_name: string | null; imap_name: string }): string {
		return folder.display_name ?? folder.imap_name;
	}

	async function selectAccount(account: typeof $currentAccount) {
		if (!account) return;
		$currentAccount = account;
		$selectedFolder = null;
		try {
			$folders = await api.folders.list(account.id);
		} catch {
			$folders = [];
		}
	}

	function selectFolder(folder: typeof $selectedFolder) {
		$selectedFolder = folder;
	}
</script>

<aside
	class="h-full flex flex-col bg-surface border-r border-border transition-all duration-200"
	class:w-64={!$sidebarCollapsed}
	class:w-14={$sidebarCollapsed}
>
	<!-- Header -->
	<div class="flex items-center justify-between p-3 border-b border-border">
		{#if !$sidebarCollapsed}
			<span class="text-sm font-semibold text-text-primary tracking-wide">MailVerdict</span>
		{/if}
		<button
			onclick={() => ($sidebarCollapsed = !$sidebarCollapsed)}
			class="p-1 rounded hover:bg-surface-light text-text-muted hover:text-text-primary transition-colors"
			aria-label="Toggle sidebar"
		>
			{#if $sidebarCollapsed}
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7"/></svg>
			{:else}
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7"/></svg>
			{/if}
		</button>
	</div>

	<!-- Account selector -->
	{#if !$sidebarCollapsed && $accounts.length > 0}
		<div class="p-2 border-b border-border">
			<select
				class="w-full bg-surface-dark text-text-primary text-xs rounded px-2 py-1.5 border border-border focus:border-accent focus:outline-none"
				onchange={(e) => {
					const target = e.target as HTMLSelectElement;
					const acct = $accounts.find((a) => a.id === target.value);
					if (acct) selectAccount(acct);
				}}
				value={$currentAccount?.id ?? ''}
			>
				{#each $accounts as acct}
					<option value={acct.id}>{acct.name}</option>
				{/each}
			</select>
		</div>
	{/if}

	<!-- Folders -->
	<nav class="flex-1 overflow-y-auto py-1">
		{#if !$sidebarCollapsed}
			{#each $foldersBySpecialUse.special as folder}
				<button
					class="w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors hover:bg-surface-light"
					class:bg-surface-light={$selectedFolder?.id === folder.id}
					class:text-accent={$selectedFolder?.id === folder.id}
					class:text-text-secondary={$selectedFolder?.id !== folder.id}
					onclick={() => selectFolder(folder)}
				>
					<span class="text-xs">{folderIcon(folder.special_use)}</span>
					<span class="truncate">{folderDisplayName(folder)}</span>
				</button>
			{/each}

			{#if $foldersBySpecialUse.regular.length > 0}
				<div class="px-3 pt-3 pb-1">
					<span class="text-[10px] uppercase tracking-wider text-text-muted font-medium">Folders</span>
				</div>
				{#each $foldersBySpecialUse.regular as folder}
					<button
						class="w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors hover:bg-surface-light"
						class:bg-surface-light={$selectedFolder?.id === folder.id}
						class:text-accent={$selectedFolder?.id === folder.id}
						class:text-text-secondary={$selectedFolder?.id !== folder.id}
						onclick={() => selectFolder(folder)}
					>
						<span class="text-xs">{folderIcon(folder.special_use)}</span>
						<span class="truncate">{folderDisplayName(folder)}</span>
					</button>
				{/each}
			{/if}
		{:else}
			{#each $foldersBySpecialUse.special as folder}
				<button
					class="w-full flex items-center justify-center py-2 transition-colors hover:bg-surface-light"
					class:bg-surface-light={$selectedFolder?.id === folder.id}
					class:text-accent={$selectedFolder?.id === folder.id}
					class:text-text-muted={$selectedFolder?.id !== folder.id}
					onclick={() => selectFolder(folder)}
					title={folderDisplayName(folder)}
				>
					<span class="text-sm">{folderIcon(folder.special_use)}</span>
				</button>
			{/each}
		{/if}
	</nav>

	<!-- Nav links -->
	{#if !$sidebarCollapsed}
		<div class="border-t border-border p-2 space-y-0.5">
			<a href="/" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors">
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/></svg>
				Dashboard
			</a>
			<a href="/search" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors">
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
				Search
			</a>
			<a href="/settings" class="flex items-center gap-2 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:bg-surface-light rounded transition-colors">
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
				Settings
			</a>
		</div>
	{/if}
</aside>
