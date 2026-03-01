<script lang="ts">
	import MailList from '../../../components/MailList.svelte';
	import MailDetailComponent from '../../../components/MailDetail.svelte';
	import { selectedMail } from '$lib/stores';
	import type { MailDetail } from '$lib/types';

	interface Props {
		data: { mail: MailDetail };
	}

	let { data }: Props = $props();

	$effect(() => {
		$selectedMail = data.mail;
	});
</script>

<div class="h-full flex">
	<!-- Mail list column -->
	<div class="w-96 flex-shrink-0 border-r border-border hidden lg:block">
		<MailList />
	</div>

	<!-- Detail panel -->
	<div class="flex-1 min-w-0">
		<MailDetailComponent mail={data.mail} />
	</div>
</div>
