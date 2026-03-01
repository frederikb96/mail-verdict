import { api } from '$lib/api';
import type { AccountResponse, FolderResponse } from '$lib/types';

export const ssr = false;

export async function load() {
	let accountList: AccountResponse[] = [];
	let folderList: FolderResponse[] = [];

	try {
		accountList = await api.accounts.list();
		if (accountList.length > 0) {
			folderList = await api.folders.list(accountList[0].id);
		}
	} catch {
		// API not available yet
	}

	return { accounts: accountList, folders: folderList };
}
