/** Jotai state atoms for SSE-driven sync state. */

import { atom } from "jotai";

export interface SyncState {
  status: string;
  can_sync: boolean;
  can_cancel: boolean;
  current_folder?: string;
  folder_index?: number;
  folder_total?: number;
  synced?: number;
  total_messages?: number;
  new_mails?: number;
  errors?: number;
  duration_s?: number;
  error_message?: string;
}

/** Per-account sync state atom. Key: account_id, value: SyncState */
export const syncStatesAtom = atom<Record<string, SyncState>>({});

/** Currently selected account ID */
export const selectedAccountIdAtom = atom<string | null>(null);

/** Currently selected folder ID */
export const selectedFolderIdAtom = atom<string | null>(null);

/** Currently selected mail ID */
export const selectedMailIdAtom = atom<string | null>(null);

/** Derived: sync state for the selected account */
export const currentSyncStateAtom = atom<SyncState | null>((get) => {
  const accountId = get(selectedAccountIdAtom);
  if (!accountId) return null;
  return get(syncStatesAtom)[accountId] ?? null;
});
