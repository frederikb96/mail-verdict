<script lang="ts">
	import MailList from '../../components/MailList.svelte';
	import { selectedMail } from '$lib/stores';
</script>

<div class="h-full flex">
	<!-- Mail list column -->
	<div class="w-96 flex-shrink-0 border-r border-border">
		<MailList />
	</div>

	<!-- Detail placeholder -->
	<div class="flex-1 flex items-center justify-center">
		{#if !$selectedMail}
			<div class="text-text-muted text-sm">Select a message to read</div>
		{/if}
	</div>
</div>
