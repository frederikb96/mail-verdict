import { writable, derived } from 'svelte/store';
import type {
	AccountResponse,
	FolderResponse,
	MailSummary,
	MailDetail
} from './types';

export const accounts = writable<AccountResponse[]>([]);
export const currentAccount = writable<AccountResponse | null>(null);
export const folders = writable<FolderResponse[]>([]);
export const selectedFolder = writable<FolderResponse | null>(null);
export const mails = writable<MailSummary[]>([]);
export const selectedMail = writable<MailDetail | null>(null);
export const sidebarCollapsed = writable(false);

export const inboxFolder = derived(folders, ($folders) =>
	$folders.find((f) => f.special_use === 'inbox' || f.imap_name === 'INBOX') ?? null
);

export const foldersBySpecialUse = derived(folders, ($folders) => {
	const special: FolderResponse[] = [];
	const regular: FolderResponse[] = [];
	for (const f of $folders) {
		if (f.special_use) {
			special.push(f);
		} else {
			regular.push(f);
		}
	}
	return { special, regular };
});
