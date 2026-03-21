<script lang="ts">
	import '../app.css';
	import Sidebar from '../components/Sidebar.svelte';
	import SearchBar from '../components/SearchBar.svelte';
	import { accounts, currentAccount, folders, selectedFolder, sidebarCollapsed } from '$lib/stores';
	import { api } from '$lib/api';
	import { sse } from '$lib/sse';
	import { onMount, onDestroy, type Snippet } from 'svelte';
	import type { SSEEvent } from '$lib/types';

	interface Props {
		data: {
			accounts: import('$lib/types').AccountResponse[];
			folders: import('$lib/types').FolderResponse[];
		};
		children: Snippet;
	}

	let { data, children }: Props = $props();
	let unsubNewMail: (() => void) | null = null;
	let unsubSyncComplete: (() => void) | null = null;

	async function refreshFolders() {
		const acct = $currentAccount;
		if (!acct) return;
		try {
			$folders = await api.folders.list(acct.id);
		} catch {
			// ignore refresh errors
		}
	}

	async function refreshAccounts() {
		try {
			const acctList = await api.accounts.list();
			$accounts = acctList;
		} catch {
			// ignore
		}
	}

	function onNewMail(_event: SSEEvent) {
		refreshFolders();
	}

	function onSyncStatus(event: SSEEvent) {
		if (event.status === 'complete') {
			refreshFolders();
			refreshAccounts();
		}
	}

	onMount(() => {
		$accounts = data.accounts;
		$folders = data.folders;
		if (data.accounts.length > 0) {
			$currentAccount = data.accounts[0];
		}
		const inbox = data.folders.find(
			(f) => f.special_use === 'inbox' || f.imap_name === 'INBOX'
		);
		if (inbox) {
			$selectedFolder = inbox;
		} else if (data.folders.length > 0) {
			$selectedFolder = data.folders[0];
		}
		sse.connect($currentAccount?.id);
		unsubNewMail = sse.on('new_mail', onNewMail);
		unsubSyncComplete = sse.on('sync_status', onSyncStatus);
	});

	onDestroy(() => {
		if (unsubNewMail) unsubNewMail();
		if (unsubSyncComplete) unsubSyncComplete();
		sse.disconnect();
	});
</script>

<div class="h-screen flex overflow-hidden bg-surface-dark">
	<!-- Sidebar -->
	<Sidebar />

	<!-- Main content area -->
	<div class="flex-1 flex flex-col min-w-0">
		<!-- Top bar -->
		<header class="flex items-center gap-3 px-4 py-2 border-b border-border bg-surface">
			<div class="flex-1 max-w-md">
				<SearchBar />
			</div>
			{#if $currentAccount}
				<span class="text-[11px] text-text-muted hidden md:block">
					{$currentAccount.name} ({$currentAccount.imap_user})
				</span>
			{/if}
		</header>

		<!-- Page content -->
		<main class="flex-1 overflow-hidden">
			{@render children()}
		</main>
	</div>
</div>
