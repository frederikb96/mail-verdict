<script lang="ts">
	interface Props {
		dkim: boolean | null;
		spf: boolean | null;
		dmarc: boolean | null;
	}

	let { dkim, spf, dmarc }: Props = $props();

	function statusClass(val: boolean | null): string {
		if (val === true) return 'bg-ham';
		if (val === false) return 'bg-spam';
		return 'bg-text-muted';
	}

	function statusLabel(val: boolean | null): string {
		if (val === true) return 'pass';
		if (val === false) return 'fail';
		return 'n/a';
	}
</script>

<div class="flex items-center gap-3">
	<div class="flex items-center gap-1" title="DKIM: {statusLabel(dkim)}">
		<span class="w-2 h-2 rounded-full {statusClass(dkim)}"></span>
		<span class="text-[10px] text-text-muted">DKIM</span>
	</div>
	<div class="flex items-center gap-1" title="SPF: {statusLabel(spf)}">
		<span class="w-2 h-2 rounded-full {statusClass(spf)}"></span>
		<span class="text-[10px] text-text-muted">SPF</span>
	</div>
	<div class="flex items-center gap-1" title="DMARC: {statusLabel(dmarc)}">
		<span class="w-2 h-2 rounded-full {statusClass(dmarc)}"></span>
		<span class="text-[10px] text-text-muted">DMARC</span>
	</div>
</div>
