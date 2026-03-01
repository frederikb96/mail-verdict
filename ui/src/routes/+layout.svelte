<script lang="ts">
	import '../app.css';
	import Sidebar from '../components/Sidebar.svelte';
	import SearchBar from '../components/SearchBar.svelte';
	import { accounts, currentAccount, folders, sidebarCollapsed } from '$lib/stores';
	import { sse } from '$lib/sse';
	import { onMount, onDestroy, type Snippet } from 'svelte';

	interface Props {
		data: {
			accounts: import('$lib/types').AccountResponse[];
			folders: import('$lib/types').FolderResponse[];
		};
		children: Snippet;
	}

	let { data, children }: Props = $props();

	onMount(() => {
		$accounts = data.accounts;
		$folders = data.folders;
		if (data.accounts.length > 0) {
			$currentAccount = data.accounts[0];
		}
		sse.connect($currentAccount?.id);
	});

	onDestroy(() => {
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
