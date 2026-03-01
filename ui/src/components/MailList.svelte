<script lang="ts">
	import { mails, selectedFolder, currentAccount, selectedMail } from '$lib/stores';
	import { api } from '$lib/api';
	import MailListItem from './MailListItem.svelte';
	import type { MailSummary } from '$lib/types';
	import { goto } from '$app/navigation';

	let loading = $state(false);
	let offset = $state(0);
	const LIMIT = 50;

	$effect(() => {
		const folder = $selectedFolder;
		const account = $currentAccount;
		if (folder && account) {
			loadMails(account.id, folder.id);
		}
	});

	async function loadMails(accountId: string, folderId: string) {
		loading = true;
		offset = 0;
		try {
			$mails = await api.mails.list({
				account_id: accountId,
				folder_id: folderId,
				limit: LIMIT,
				offset: 0
			});
		} catch {
			$mails = [];
		} finally {
			loading = false;
		}
	}

	async function loadMore() {
		const account = $currentAccount;
		const folder = $selectedFolder;
		if (!account || !folder) return;
		offset += LIMIT;
		try {
			const more = await api.mails.list({
				account_id: account.id,
				folder_id: folder.id,
				limit: LIMIT,
				offset
			});
			$mails = [...$mails, ...more];
		} catch {
			// keep existing
		}
	}

	function openMail(mail: MailSummary) {
		goto(`/mail/${mail.id}?account_id=${mail.account_id}`);
	}

	function folderDisplayName(): string {
		const f = $selectedFolder;
		if (!f) return 'Select a folder';
		return f.display_name ?? f.imap_name;
	}
</script>

<div class="h-full flex flex-col bg-surface-dark">
	<!-- Header -->
	<div class="px-3 py-2 border-b border-border flex items-center justify-between">
		<h2 class="text-sm font-medium text-text-primary">{folderDisplayName()}</h2>
		<span class="text-[11px] text-text-muted">{$mails.length} messages</span>
	</div>

	<!-- Mail list -->
	<div class="flex-1 overflow-y-auto">
		{#if loading}
			<div class="flex items-center justify-center py-12 text-text-muted text-sm">
				Loading...
			</div>
		{:else if $mails.length === 0}
			<div class="flex items-center justify-center py-12 text-text-muted text-sm">
				{#if $selectedFolder}
					No messages
				{:else}
					Select a folder
				{/if}
			</div>
		{:else}
			{#each $mails as mail (mail.id)}
				<MailListItem
					{mail}
					selected={$selectedMail?.id === mail.id}
					onclick={() => openMail(mail)}
				/>
			{/each}

			{#if $mails.length >= offset + LIMIT}
				<button
					class="w-full py-3 text-xs text-accent hover:text-accent-hover transition-colors"
					onclick={loadMore}
				>
					Load more
				</button>
			{/if}
		{/if}
	</div>
</div>
