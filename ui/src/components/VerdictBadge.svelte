<script lang="ts">
	import type { VerdictResponse } from '$lib/types';
	import { api } from '$lib/api';

	interface Props {
		verdict: VerdictResponse | null;
		mailId: string;
		accountId: string;
		onupdate?: () => void;
	}

	let { verdict, mailId, accountId, onupdate }: Props = $props();
	let submitting = $state(false);

	async function submitFeedback(isSpam: boolean) {
		submitting = true;
		try {
			await api.verdicts.feedback(mailId, accountId, isSpam);
			onupdate?.();
		} catch {
			// ignore
		} finally {
			submitting = false;
		}
	}
</script>

<div class="flex items-center gap-3">
	{#if verdict}
		<span
			class={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
				verdict.is_spam ? 'bg-spam/15 text-spam' : 'bg-ham/15 text-ham'
			}`}
		>
			{#if verdict.is_spam}
				<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>
				Spam
			{:else}
				<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>
				Ham
			{/if}
		</span>

		{#if verdict.source}
			<span class="text-[10px] text-text-muted">via {verdict.source}</span>
		{/if}

		<!-- Feedback buttons -->
		<div class="flex items-center gap-1 ml-2">
			{#if verdict.is_spam}
				<button
					class="text-[11px] px-2 py-0.5 rounded border border-ham/30 text-ham hover:bg-ham/10 transition-colors disabled:opacity-50"
					disabled={submitting}
					onclick={() => submitFeedback(false)}
				>
					Not spam
				</button>
			{:else}
				<button
					class="text-[11px] px-2 py-0.5 rounded border border-spam/30 text-spam hover:bg-spam/10 transition-colors disabled:opacity-50"
					disabled={submitting}
					onclick={() => submitFeedback(true)}
				>
					Mark spam
				</button>
			{/if}
		</div>
	{:else}
		<span class="text-xs text-text-muted">No verdict</span>
	{/if}
</div>

{#if verdict?.reasoning}
	<p class="mt-2 text-xs text-text-secondary leading-relaxed">{verdict.reasoning}</p>
{/if}
